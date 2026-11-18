import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import httpx
import pytest
import yaml
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pypdf import PdfReader

from isakisi import applications
from isakisi.catalog import job_rows
from isakisi.db import Store, stamp
from isakisi.maintenance import backup, restore
from isakisi.profile import load_profile, save_profile
from isakisi.queue import schedule, worker
from isakisi.settings import Problem, Settings, Source
from isakisi.sources import scan
from isakisi.web import create_app, login_token


def csrf(page):
    return BeautifulSoup(page.text, "html.parser").select_one("input[name=csrf]")["value"]


def test_manual_end_to_end(seeded):
    client = TestClient(create_app(seeded), base_url="http://127.0.0.1", follow_redirects=False)
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/jobs").status_code == 303
    token = csrf(client.get("/login"))
    assert client.post("/login", data={"token": login_token(seeded.session_key())}).status_code == 409
    assert client.post("/login", data={"csrf": token, "token": login_token(seeded.session_key())}).status_code == 303
    jobs = client.get("/jobs")
    assert "Python Backend Geliştirici" in jobs.text and "text/html" in jobs.headers["content-type"]
    assert client.get("/sources").status_code == 200
    assert client.get("/profile").status_code == 200
    token = csrf(jobs)
    response = client.post("/jobs/new", data={"csrf": token, "title": "Python Engineer", "company": "Örnek Şirket",
                                             "url": "https://example.com/careers/python", "language": "en",
                                             "description": "Requirements\nPython, Docker and SQL"})
    assert response.status_code == 303
    job_page = response.headers["location"]
    response = client.post(job_page + "/prepare", data={"csrf": token, "route": "manual"})
    assert response.status_code == 303
    app_url = response.headers["location"]
    page = client.get(app_url)
    soup = BeautifulSoup(page.text, "html.parser")
    assert "Doğrulanmamış sertifika" not in page.text
    assert "Dear Örnek Şirket" in page.text
    bundle_id = soup.select_one("input[name=bundle]")["value"]
    package_hash = soup.select_one("input[name=digest]")["value"]
    export = client.get(f"{app_url}/documents/cv.pdf?bundle={bundle_id}")
    assert export.status_code == 200
    pdf = "\n".join(page.extract_text() for page in PdfReader(BytesIO(export.content)).pages)
    assert "Deniz Öztürk" in pdf and "SQL" in pdf and "Doğrulanmamış" not in pdf
    edited = client.post(app_url + "/edit", data={"csrf": token, "bundle": bundle_id, "cover": "User reviewed cover letter."})
    assert edited.status_code == 303
    archived = client.get(f"{app_url}?version={bundle_id}")
    assert "Önceki sürüm görüntüleniyor" in archived.text and 'name="confirm"' not in archived.text
    assert client.post(app_url + "/approve", data={"csrf": token, "bundle": bundle_id, "digest": package_hash,
                                                  "confirm": "yes"}).status_code == 409
    page = client.get(app_url)
    soup = BeautifulSoup(page.text, "html.parser")
    bundle_id = soup.select_one("input[name=bundle]")["value"]
    package_hash = soup.select_one("input[name=digest]")["value"]
    assert client.post(app_url + "/approve", data={"csrf": token, "bundle": bundle_id,
                                                  "digest": package_hash, "confirm": "yes"}).status_code == 303
    assert "Resmî formu aç" in client.get(app_url).text
    assert client.post(app_url + "/record-result", data={"csrf": token, "state": "submitted",
                                                        "note": "Resmî formdan gönderildi."}).status_code == 303
    assert "Kullanıcı teyidi" in client.get(app_url).text
    assert client.post(app_url + "/record-result", data={"csrf": token, "state": "interview"}).status_code == 303
    assert "Görüşme" in client.get("/applications").text


def test_profile_version_edit_invalidates_package(seeded):
    app_id = applications.prepare(seeded, 1)
    with seeded.connect(write=True) as conn:
        _, bundle, _ = applications.get_app(conn, app_id)
        current = load_profile(conn)
        save_profile(conn, current["identity"], current["facts"], [])
    with pytest.raises(Problem, match="İlan veya profil değişti"):
        applications.approve(seeded, app_id, bundle["id"], bundle["digest"])


def test_concurrent_simulation_approval_only_one_receipt(seeded):
    app_id = applications.prepare(seeded, 1)
    with seeded.connect() as conn:
        _, bundle, _ = applications.get_app(conn, app_id)

    def approve():
        try:
            applications.approve(seeded, app_id, bundle["id"], bundle["digest"])
        except Problem:
            return 0
        return 1

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(lambda _: approve(), range(2))) == 1
        list(pool.map(lambda _: worker(seeded, once=True), range(2)))
    with seeded.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM simulated_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
        assert applications.get_app(conn, app_id)[0]["state"] == "simulated"


def test_chosen_consent_and_supported_answers_are_bound_to_approval(employer):
    store, api, schema, _ = employer
    schema["questions"] = [{"label": "Describe your Python experience", "required": True,
                            "fields": [{"name": "question_1", "type": "textarea"}]}]
    schema["data_compliance"] = [{"type": "gdpr", "requires_processing_consent": True}]
    app_id = applications.prepare(store, 1, client=api)
    with store.connect() as conn:
        _, bundle, _ = applications.get_app(conn, app_id)
    payload = json.loads(bundle["payload"])
    assert "Python" in payload["answers"][0]
    assert payload["answer_fact_ids"][0]
    with pytest.raises(Problem, match="rıza"):
        applications.approve(store, app_id, bundle["id"], bundle["digest"])
    applications.edit_bundle(store, app_id, bundle["id"], payload["cover"], payload["answers"],
                             {"gdpr_processing_consent_given": "no"})
    with store.connect() as conn:
        _, newer, _ = applications.get_app(conn, app_id)
    assert newer["digest"] != bundle["digest"]
    applications.approve(store, app_id, newer["id"], newer["digest"])


@pytest.fixture
def employer(candidate, tmp_path, monkeypatch):
    source = Source(id="employer", platform="greenhouse", company="Acme", board="test",
                    hosts=["boards-api.greenhouse.io"], permission_url="https://example.invalid/permission",
                    checked_on="2026-11-18", submit=True, secret_env="ISAKISI_TEST_KEY")
    config = tmp_path / "sources.yaml"
    config.write_text(yaml.safe_dump({"sources": [source.model_dump()]}), encoding="utf-8")
    monkeypatch.setenv("ISAKISI_TEST_KEY", "synthetic-private-key")
    store = Store(Settings(app_data_dir=candidate.root, discovery_mode="live", submit_mode="authorized", source_config=config))
    job = {"id": 100, "title": "Backend Engineer", "absolute_url": "https://job-boards.greenhouse.io/test/jobs/100",
           "content": "Requirements\nPython required"}
    schema = {"id": 100, "questions": []}
    posts = []

    def handler(req):
        assert req.url.host == "boards-api.greenhouse.io"
        if req.method == "GET":
            if req.url.params.get("questions"):
                return httpx.Response(200, json=schema)
            return httpx.Response(200, json={"jobs": [job]})
        posts.append(req)
        raise httpx.ReadError("simulated response loss")

    with httpx.Client(transport=httpx.MockTransport(handler)) as api:
        scan(store, source, api)
        yield store, api, schema, posts


def test_authorized_response_loss_does_not_replay(employer):
    store, api, _, posts = employer
    app_id = applications.prepare(store, 1, client=api)
    with store.connect() as conn:
        _, bundle, _ = applications.get_app(conn, app_id)
    applications.approve(store, app_id, bundle["id"], bundle["digest"])
    with store.connect() as conn:
        attempt = conn.execute("SELECT id FROM submission_attempts").fetchone()[0]
    applications.run_attempt(store, attempt, client=api)
    reopened = Store(store.settings)
    reopened.initialize()
    applications.run_attempt(reopened, attempt, client=api)
    with reopened.connect() as conn:
        assert len(posts) == 1
        assert applications.get_app(conn, app_id)[0]["state"] == "submission_unknown"
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='reconcile'").fetchone()[0] == 1
    with pytest.raises(Problem):
        applications.prepare(reopened, 1, manual=True)


def test_changed_form_is_rejected_before_post(employer):
    store, api, schema, posts = employer
    app_id = applications.prepare(store, 1, client=api)
    with store.connect() as conn:
        _, bundle, _ = applications.get_app(conn, app_id)
    applications.approve(store, app_id, bundle["id"], bundle["digest"])
    schema["questions"].append({"label": "New question", "required": True,
                                "fields": [{"name": "question_42", "type": "input_text"}]})
    with store.connect() as conn:
        attempt = conn.execute("SELECT id FROM submission_attempts").fetchone()[0]
    applications.run_attempt(store, attempt, client=api)
    with store.connect() as conn:
        assert applications.get_app(conn, app_id)[0]["state"] == "attention"
    assert not posts


def test_interrupted_send_is_reconciled_without_replay(employer):
    store, api, _, posts = employer
    app_id = applications.prepare(store, 1, client=api)
    with store.connect() as conn:
        _, bundle, _ = applications.get_app(conn, app_id)
    applications.approve(store, app_id, bundle["id"], bundle["digest"])
    with store.connect(write=True) as conn:
        conn.execute("UPDATE submission_attempts SET state='submitting', updated=?", (stamp(-600),))
        conn.execute("UPDATE applications SET state='submitting'")
        conn.execute("UPDATE tasks SET state='running', lease_until=? WHERE kind='submit'", (stamp(-600),))
    worker(store, once=True)
    with store.connect() as conn:
        assert applications.get_app(conn, app_id)[0]["state"] == "submission_unknown"
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='submit'").fetchone()[0] == 1
    assert not posts


def test_restore_verifies_blobs_and_disables_outbound(seeded, tmp_path):
    app_id = applications.prepare(seeded, 1)
    with seeded.connect() as conn:
        _, bundle, _ = applications.get_app(conn, app_id)
    applications.approve(seeded, app_id, bundle["id"], bundle["digest"])
    key = Fernet.generate_key().decode()
    archive = tmp_path / "backup.enc"
    backup(seeded, archive, key)
    newer = Store(Settings(app_data_dir=tmp_path / "new", _env_file=None))
    newer.initialize()
    with pytest.raises(Problem):
        restore(newer, archive, Fernet.generate_key().decode())
    restore(newer, archive, key)
    worker(newer, once=True)
    with newer.connect() as conn:
        record, bundle, _ = applications.get_app(conn, app_id)
        assert record["state"] == "submission_unknown"
        assert conn.execute("SELECT value FROM control WHERE key='restore_lock'").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM simulated_receipts").fetchone()[0] == 0
        assert newer.get(json.loads(bundle["payload"])["documents"]["cv.pdf"]).startswith(b"%PDF-")


def test_expired_read_task_is_reclaimed(store):
    schedule(store)
    with store.connect(write=True) as conn:
        conn.execute("UPDATE tasks SET state='running',attempts=1,lease_until=?", (stamp(-600),))
    worker(store, once=True)
    with store.connect() as conn:
        assert len(job_rows(conn)) == 6
        assert tuple(conn.execute("SELECT state,attempts FROM tasks").fetchone()) == ("done", 2)
