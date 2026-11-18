import json
import uuid
from contextlib import nullcontext

import httpx

from .catalog import SKILLS, contains, job_rows
from .db import digest, notify, pack, stamp
from .documents import cover_letter, docx_bytes, lines_for, pdf_bytes
from .matching import assess
from .profile import load_profile
from .settings import Problem, allowed_url
from .submission import (SIM_URL, api_target, browser_prefill, consent_fields, fetch_schema, preflight, route_for,
                         send_greenhouse, simulate, source_for, supported_questions, validate_answers)

FOLLOW_UP = {
    "submitted": {"interview", "offer", "rejected", "withdrawn"},
    "interview": {"offer", "rejected", "withdrawn"},
    "offer": {"rejected", "withdrawn"},
    "submission_unknown": {"submitted", "withdrawn"},
    "partial": {"submitted", "withdrawn"},
    "approved_manual": {"submitted", "withdrawn"},
}
BUSY_OR_FINAL = {"queued", "submitting", "submission_unknown", "partial", "submitted",
                 "interview", "offer", "rejected", "withdrawn", "simulated"}


def event(conn, app_id, state, evidence, note=""):
    now = stamp()
    conn.execute("UPDATE applications SET state=?,updated=? WHERE id=?", (state, now, app_id))
    conn.execute("INSERT INTO application_events(application_id,state,evidence,note,created) VALUES (?,?,?,?,?)",
                 (app_id, state, evidence, note, now))
    if state in {"submission_unknown", "partial", "approved_manual", "attention"}:
        labels = {"submission_unknown": "Gönderim teyidi gerekli", "partial": "Form sonucu bekleniyor",
                  "approved_manual": "Resmî form bekleniyor", "attention": "Başvuru paketini kontrol edin"}
        notify(conn, f"app:{app_id}:{state}:{now}", labels[state], f"/applications/{app_id}")


def get_app(conn, app_id):
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not app:
        raise Problem("Başvuru bulunamadı.")
    bundle = conn.execute("SELECT * FROM bundles WHERE application_id=? ORDER BY id DESC LIMIT 1", (app_id,)).fetchone()
    job = job_rows(conn)[app["opportunity_id"]]
    return dict(app), dict(bundle) if bundle else None, job


def ensure_app(conn, opportunity):
    now = stamp()
    conn.execute("INSERT OR IGNORE INTO profiles VALUES (1)")
    conn.execute("INSERT OR IGNORE INTO applications(profile_id,opportunity_id,state,created,updated) VALUES(1,?,'draft',?,?)",
                 (opportunity, now, now))
    return conn.execute("SELECT * FROM applications WHERE profile_id=1 AND opportunity_id=?", (opportunity,)).fetchone()


def new_bundle(conn, app_id, job, profile_id, payload):
    checked = digest(payload)
    bundle_id = conn.execute("""INSERT INTO bundles(application_id,posting_version_id,profile_version_id,digest,payload,created)
            VALUES (?,?,?,?,?,?)""", (app_id, job["version_id"], profile_id, checked, pack(payload), stamp())).lastrowid
    conn.executemany("INSERT INTO documents VALUES (?,?,?)",
                     [(bundle_id, name, sha) for name, sha in payload["documents"].items()])
    conn.execute("""UPDATE approvals SET revoked=? WHERE consumed IS NULL AND revoked IS NULL
        AND bundle_id IN (SELECT id FROM bundles WHERE application_id=?)""", (stamp(), app_id))
    event(conn, app_id, "draft", "package", f"Paket sürümü {bundle_id}")
    return bundle_id


def suggest_answers(questions, facts):
    answers, sources = [], []
    for question in questions:
        label = question["label"]
        requested = [aliases for aliases in SKILLS.values() if any(contains(label, word) for word in aliases)]
        open_ended = any(contains(label, phrase) for phrase in ("describe", "tell us about", "explain", "açıklayın", "anlatın"))
        selected = [
            fact for fact in facts if fact["kind"] in {"experience", "project", "skill"}
            and open_ended and requested and not question.get("values")
            and any(contains(fact["text"], alias) for group in requested for alias in group)
        ][:2]
        answers.append("\n".join(fact["text"] for fact in selected))
        sources.append([fact["id"] for fact in selected])
    return answers, sources


def answer_text(payload):
    return "\n\n".join(f"{question['label']}\n{answer}"
                       for question, answer in zip(payload["questions"], payload["answers"], strict=True))


def prepare(store, opportunity, *, manual=False, client=None):
    with store.connect() as conn:
        job = job_rows(conn).get(opportunity)
        profile = load_profile(conn)
        if not job or job["state"] != "open" or job["duplicate_of"]:
            raise Problem("Paket için açık ve kopya incelemesi tamamlanmış bir ilan gerekli.")
        route = "manual" if manual else route_for(store, conn, job)
    identity = profile["identity"]
    facts = [f for f in profile["facts"] if f["verified_at"]]
    if not identity["name"].strip() or not facts:
        raise Problem("Önce profil adını ve en az bir doğrulanmış olguyu kaydedin.")
    item = job["item"]
    matched = assess(item, profile)
    relevant = {f["id"] for reason in matched["reasons"] for f in reason["facts"]}
    facts.sort(key=lambda f: (f["language"] not in {"und", item["language"]}, f["id"] not in relevant))
    schema = None
    prefill_fields = {}
    consents = {}
    questions = item["questions"]
    target = item["apply_url"] or item["url"]
    if route == "simulate":
        target = SIM_URL
    elif route in {"authorized", "prefill"}:
        source = source_for(store.settings, job)
        if route == "authorized":
            context = nullcontext(client) if client is not None else httpx.Client(timeout=15, trust_env=False)
            with context as api:
                schema = fetch_schema(source, job, api)
            questions = supported_questions(schema)
            consents = consent_fields(schema)
            target = api_target(source, job)
        else:
            target = allowed_url(target, source.hosts)
            prefill_fields = source.prefill_fields
    from .models import select_facts

    suggestion = select_facts(store.settings, item, facts)
    cover = cover_letter(identity, item, facts, suggestion)
    lines = lines_for(identity, item, facts)
    answers, answer_facts = suggest_answers(questions, facts)
    payload = {"job": {"company": item["company"], "title": item["title"], "url": item["url"]},
               "target": target, "route": route, "schema": schema, "prefill_fields": prefill_fields,
               "source_id": job["source_id"], "posting_version": job["version_id"], "profile_version": profile["id"],
               "language": item["language"], "identity": identity, "questions": questions, "answers": answers,
               "answer_fact_ids": answer_facts, "consent_fields": consents,
               "fact_ids": [f["id"] for f in facts], "cover": cover, "reviewed_cover": False,
               "cv_lines": lines, "consent": {}, "documents": {
                   "cv.pdf": store.put(pdf_bytes(store.settings, lines)),
                   "cv.docx": store.put(docx_bytes(lines)),
                   "on-yazi.txt": store.put(cover.encode()),
               }}
    payload["documents"]["yanitlar.txt"] = store.put(answer_text(payload).encode())
    with store.connect(write=True) as conn:
        fresh = job_rows(conn).get(opportunity)
        if (load_profile(conn)["id"] != profile["id"] or fresh["version_id"] != job["version_id"]
                or fresh["state"] != "open" or fresh["duplicate_of"]
                or (route != "manual" and route_for(store, conn, fresh) != route)):
            raise Problem("İlan, profil veya izinler değişti. Paketi yeniden hazırlayın.")
        app = ensure_app(conn, opportunity)
        if app["state"] in BUSY_OR_FINAL:
            raise Problem("Bu başvurunun açık veya tamamlanmış bir gönderim kaydı var.")
        new_bundle(conn, app["id"], job, profile["id"], payload)
    return app["id"]


def edit_bundle(store, app_id, bundle_id, cover, answers, consent=None):
    if len(cover) > 12000 or any(len(answer) > 4000 for answer in answers):
        raise Problem("Ön yazı veya form yanıtı boyut sınırını aştı.")
    with store.connect(write=True) as conn:
        app, bundle, job = get_app(conn, app_id)
        payload = check_current(conn, app, bundle, job, bundle_id)
        if len(answers) != len(payload["questions"]):
            raise Problem("Form alanları değişti. Sayfayı yenileyin.")
        payload["reviewed_cover"] = cover != payload["cover"] or payload["reviewed_cover"]
        payload["cover"] = cover
        payload["answer_fact_ids"] = [ids if old == new else [] for ids, old, new in
                                      zip(payload["answer_fact_ids"], payload["answers"], answers, strict=True)]
        payload["answers"] = answers
        payload["consent"] = {key: (consent or {}).get(key, "") for key in payload.get("consent_fields", {})}
        validate_answers(payload)
        payload["documents"]["on-yazi.txt"] = store.put(cover.encode())
        payload["documents"]["yanitlar.txt"] = store.put(answer_text(payload).encode())
        return new_bundle(conn, app_id, job, bundle["profile_version_id"], payload)


def check_current(conn, app, bundle, job, bundle_id):
    if not bundle or bundle["id"] != bundle_id:
        raise Problem("Paket değişti. En son sürümü açın.")
    if app["state"] in BUSY_OR_FINAL:
        raise Problem("Bu başvuru için işlem zaten kaydedildi.")
    if (job["state"] != "open" or job["duplicate_of"] or job["version_id"] != bundle["posting_version_id"]
            or load_profile(conn)["id"] != bundle["profile_version_id"]):
        raise Problem("İlan veya profil değişti. Paketi yeniden hazırlayın.")
    payload = json.loads(bundle["payload"])
    if digest(payload) != bundle["digest"]:
        raise Problem("Paket bütünlüğü doğrulanamadı.")
    return payload


def approve(store, app_id, bundle_id, confirmed_digest):
    from .queue import enqueue

    with store.connect(write=True) as conn:
        app, bundle, job = get_app(conn, app_id)
        payload = check_current(conn, app, bundle, job, bundle_id)
        route = payload["route"]
        if bundle["digest"] != confirmed_digest:
            raise Problem("Onay, sayfada görülen paketle eşleşmiyor.")
        if route != "manual" and route_for(store, conn, job) != route:
            raise Problem("Başvuru izni değişti. Paketi yeniden hazırlayın.")
        if route in {"authorized", "prefill"} and not payload["identity"]["email"]:
            raise Problem("Gönderim için profil e-postası gerekli.")
        if route == "authorized" and len(payload["identity"]["name"].strip().split(maxsplit=1)) < 2:
            raise Problem("Gönderim için ad ve soyad gerekli.")
        validate_answers(payload, mandatory=route == "authorized")
        for sha in payload["documents"].values():
            store.get(sha)
        now = stamp()
        if route == "manual" and conn.execute(
            "SELECT 1 FROM approvals WHERE bundle_id=? AND expires>? AND revoked IS NULL", (bundle_id, now)
        ).fetchone():
            return
        approval = conn.execute("INSERT INTO approvals(bundle_id,digest,target,route,created,expires,consumed) VALUES(?,?,?,?,?,?,?)",
                                (bundle_id, bundle["digest"], payload["target"], route, now, stamp(86400), now)).lastrowid
        if route == "manual":
            event(conn, app_id, "approved_manual", "approval")
            return
        attempt = uuid.uuid4().hex
        target_key = digest({"route": route, "target": payload["target"], "external": job["external_key"]})
        conn.execute("INSERT INTO submission_attempts VALUES (?,?,?,?,?,'queued',NULL,?,?)",
                     (attempt, approval, app_id, target_key, route, now, now))
        event(conn, app_id, "queued", "approval")
        enqueue(conn, f"submit:{attempt}", "submit", {"attempt": attempt})


def run_attempt(store, attempt_id, *, client=None, dispatch=None):
    context = nullcontext(client) if client is not None else httpx.Client(timeout=15, trust_env=False)
    with context as api:
        with store.connect(write=True) as conn:
            row = conn.execute("""SELECT a.*,p.expires,p.bundle_id,p.digest,p.target FROM submission_attempts a
                JOIN approvals p ON p.id=a.approval_id WHERE a.id=? AND a.state='queued'""", (attempt_id,)).fetchone()
            if not row:
                return
            attempt = dict(row)
            app, bundle, job = get_app(conn, attempt["application_id"])
            conn.execute("UPDATE submission_attempts SET state='checking',updated=? WHERE id=?", (stamp(), attempt_id))
        try:
            payload = json.loads(bundle["payload"])
            if (attempt["expires"] <= stamp() or bundle["id"] != attempt["bundle_id"]
                    or bundle["digest"] != attempt["digest"] or attempt["digest"] != digest(payload)
                    or payload["target"] != attempt["target"]):
                raise Problem("Onay süresi veya onaylanan paket değişti.")
            route = attempt["route"]
            source = preflight(store, job, payload, route, api)
            for sha in payload["documents"].values():
                store.get(sha)
            resume = store.get(payload["documents"]["cv.pdf"])
            with store.connect(write=True) as conn:
                app, current, job = get_app(conn, app["id"])
                if (app["state"] != "queued" or current["id"] != bundle["id"]
                        or job["version_id"] != bundle["posting_version_id"] or job["state"] != "open" or job["duplicate_of"]
                        or load_profile(conn)["id"] != bundle["profile_version_id"] or route_for(store, conn, job) != route
                        or attempt["expires"] <= stamp()):
                    raise Problem("İlan, profil veya başvuru izni değişti.")
                changed = conn.execute("UPDATE submission_attempts SET state='submitting',updated=? WHERE id=? AND state='checking'",
                                       (stamp(), attempt_id)).rowcount
                if not changed:
                    return
                event(conn, app["id"], "submitting", "worker")
            if route in {"authorized", "prefill"}:
                with store.connect(write=True, ledger=True) as ledger:
                    claimed = ledger.execute("INSERT OR IGNORE INTO outgoing VALUES (?,?,?)",
                                             (attempt["target_key"], attempt_id, stamp())).rowcount
                if not claimed:
                    finish(store, attempt, "submission_unknown", "", "Bu hedef için önceki gönderim kaydı var.")
                    return
        except Problem as error:
            finish(store, attempt, "attention", "", str(error))
            return
        except Exception:
            finish(store, attempt, "submission_unknown", "", "Gönderim adımı uzlaştırma bekliyor.")
            return
        try:
            if dispatch:
                state, receipt = dispatch(store, attempt, payload)
            elif route == "simulate":
                state, receipt = simulate(store, attempt_id, payload)
            elif route == "authorized":
                state, receipt = send_greenhouse(api, source, payload, resume)
            else:
                state, receipt = browser_prefill(store, source, payload, resume)
        except Exception:
            state, receipt = "submission_unknown", ""
        finish(store, attempt, state, receipt)


def finish(store, attempt, state, receipt, note=""):
    with store.connect(write=True) as conn:
        updated = conn.execute("""UPDATE submission_attempts SET state=?,receipt=?,updated=?
              WHERE id=? AND state IN ('checking','submitting','submission_unknown')""",
                               (state, receipt or None, stamp(), attempt["id"])).rowcount
        if updated:
            evidence = {"submitted": "provider_receipt", "simulated": "simulation_receipt",
                        "partial": "browser_handoff"}.get(state, "worker")
            event(conn, attempt["application_id"], state, evidence, note or receipt)
            if state == "submission_unknown":
                from .queue import enqueue
                enqueue(conn, f"reconcile:{attempt['id']}", "reconcile", {"attempt": attempt["id"]})


def recover_attempts(store):
    with store.connect() as conn:
        attempts = [dict(row) for row in conn.execute("""SELECT * FROM submission_attempts
                    WHERE state IN ('checking','submitting') AND updated<?""", (stamp(-300),))]
    for attempt in attempts:
        finish(store, attempt, "submission_unknown", "", "İşçi kesildi. Başvuru yeniden gönderilmeyecek.")
    with store.connect(write=True) as conn:
        queued = conn.execute("""SELECT a.id,a.application_id FROM submission_attempts a
                JOIN tasks t ON t.key='submit:' || a.id WHERE a.state='queued' AND t.state='failed'""").fetchall()
        for attempt, app_id in queued:
            conn.execute("UPDATE submission_attempts SET state='attention',updated=? WHERE id=?", (stamp(), attempt))
            event(conn, app_id, "attention", "worker", "İşçi gönderim başlamadan kesildi. Paketi yeniden hazırlayın.")


def reconcile(store, attempt_id):
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM submission_attempts WHERE id=? AND state='submission_unknown'", (attempt_id,)).fetchone()
        receipt = conn.execute("SELECT receipt FROM simulated_receipts WHERE attempt=?", (attempt_id,)).fetchone()
    if row and row["route"] == "simulate" and receipt:
        finish(store, dict(row), "simulated", receipt[0])


def record_result(conn, app_id, state, note):
    app, _, _ = get_app(conn, app_id)
    if state not in FOLLOW_UP.get(app["state"], set()):
        raise Problem("Bu başvuru durumu için seçilen geçiş kullanılamıyor.")
    if state == "submitted":
        conn.execute("UPDATE submission_attempts SET state='confirmed_by_user',updated=? WHERE application_id=? AND state IN ('submission_unknown','partial')",
                     (stamp(), app_id))
    event(conn, app_id, state, "user_confirmation", note[:1000])


def record_prior(conn, job_id, note):
    job = job_rows(conn).get(job_id)
    if not job or job["duplicate_of"]:
        raise Problem("Önce ilan kimliğini ve olası kopyayı kontrol edin.")
    app = ensure_app(conn, job_id)
    if app["state"] in BUSY_OR_FINAL:
        raise Problem("Bu ilan için zaten bir başvuru kaydı var.")
    event(conn, app["id"], "submitted", "user_confirmation", note[:1000])
    return app["id"]
