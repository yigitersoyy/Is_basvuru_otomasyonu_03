import hashlib
import hmac
import json
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from . import applications, catalog, matching, profile
from .db import stamp
from .queue import schedule
from .settings import Problem

ROOT = Path(__file__).parent / "web_assets"
STATES = {
    "draft": "Taslak", "approved_manual": "Resmî form bekliyor", "queued": "Onaylandı, sırada",
    "submitting": "Gönderim işleniyor", "attention": "Paket kontrolü gerekli", "submitted": "Gönderildi",
    "partial": "Form sonucu bekleniyor", "submission_unknown": "Sonuç belirsiz", "simulated": "Simülasyon tamamlandı",
    "interview": "Görüşme", "offer": "Teklif", "rejected": "Ret", "withdrawn": "Geri çekildi",
}
ROUTES = {
    "manual": "Resmî formu ben tamamlayacağım", "simulate": "Yerel simülasyon",
    "authorized": "Yetkili API ile gönderim", "prefill": "İzinli tarayıcı doldurma",
}
FACTS = {"skill": "Beceri", "experience": "Deneyim", "project": "Proje", "education": "Eğitim",
         "language": "Dil", "certification": "Sertifika", "other": "Diğer"}
EVIDENCE = {"met": "Karşılandı", "conflict": "Çelişiyor", "unknown": "Bilgi eksik", "missing": "Profilde kanıt yok"}
EVENTS = {"approval": "Paket onayı", "package": "Paket sürümü", "provider_receipt": "Sağlayıcı makbuzu",
          "simulation_receipt": "Sahte ATS makbuzu", "browser_handoff": "Tarayıcı devri", "user_confirmation": "Kullanıcı teyidi",
          "worker": "İşçi kaydı", "restore": "Yedekten dönüş"}


def login_token(secret):
    return hmac.new(secret.encode(), b"isakisi-local-login", hashlib.sha256).hexdigest()[:32]


def create_app(store):
    store.initialize()
    settings = store.settings
    secret = store.session_key()
    timezone = ZoneInfo(settings.app_timezone)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    views = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))

    def local_date(value):
        if not value:
            return "—"
        try:
            return datetime.fromisoformat(value).astimezone(timezone).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return value

    views.filters["local"] = local_date
    views.globals.update(states=STATES, routes=ROUTES, fact_labels=FACTS, pref_labels=matching.LABELS,
                         evidence_labels=EVIDENCE, event_labels=EVENTS, demo=settings.discovery_mode == "fixture")

    def view(request, name, **values):
        token = request.session.setdefault("csrf", secrets.token_urlsafe(24))
        status_code = values.pop("status_code", 200)
        return HTMLResponse(views.get_template(name + ".html").render(
            request=request, csrf=token, path=request.url.path, authenticated=request.session.get("auth"),
            flash=request.session.pop("flash", ""), **values), status_code)

    def go(path, request=None, flash=""):
        if request and flash:
            request.session["flash"] = flash
        return RedirectResponse(path, status_code=303)

    async def form(request):
        if int(request.headers.get("content-length", "0")) > 10 * 1024 * 1024:
            raise Problem("Yükleme boyut sınırı aşıldı.")
        values = await request.form(max_fields=5000, max_part_size=8 * 1024 * 1024, max_files=1)
        token = str(values.get("csrf") or "")
        expected = request.session.get("csrf")
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise Problem("Oturumu yenileyip işlemi tekrar deneyin.")
        return values

    @app.middleware("http")
    async def guard(request, call_next):
        path = request.url.path
        if path not in {"/login", "/healthz"} and not path.startswith("/static/") and not request.session.get("auth"):
            response = go("/login")
        else:
            response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="isakisi_session", same_site="strict", max_age=86400)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.exception_handler(Problem)
    async def domain_error(request, error):
        return view(request, "error", error=str(error), status_code=409)

    @app.exception_handler(ValidationError)
    async def invalid_fields(request, error):
        return view(request, "error", error="Girdi alanlarını kontrol edin.", status_code=422)

    @app.get("/login")
    def login(request: Request):
        return view(request, "login")

    @app.post("/login")
    async def authenticate(request: Request):
        values = await form(request)
        candidate = str(values.get("token") or "").strip().encode()
        if not hmac.compare_digest(candidate, login_token(secret).encode()):
            return view(request, "login", error="Giriş kodunu kontrol edin.", status_code=403)
        request.session.clear()
        request.session["auth"] = True
        return go("/jobs")

    @app.post("/logout")
    async def logout(request: Request):
        await form(request)
        request.session.clear()
        return go("/login")

    @app.get("/healthz")
    def health():
        with store.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return JSONResponse({"status": "ok"})

    @app.get("/")
    def home():
        return go("/jobs")

    @app.get("/jobs")
    def jobs(request: Request, q: str = "", filter: str = "open"):
        with store.connect() as conn:
            all_jobs = matching.ranked(conn)
            applications_count = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
            alerts = conn.execute("SELECT COUNT(*) FROM notifications WHERE seen IS NULL").fetchone()[0]
        query = profile.key(q)
        filters = {"open", "priority", "review", "all"}
        filter = filter if filter in filters else "open"
        counts = {
            "open": sum(j["state"] == "open" for j in all_jobs),
            "priority": sum(j["state"] == "open" and not j["duplicate_of"] and (j["match"] or {}).get("priority", False) for j in all_jobs),
            "review": sum(j["state"] != "closed" and (bool(j["duplicate_of"]) or (j["match"] or {}).get("review", False)) for j in all_jobs),
        }
        results = []
        for job in all_jobs:
            match = job["match"] or {}
            item = job["item"]
            include = (filter == "all" or (filter == "open" and job["state"] == "open")
                       or (filter == "priority" and job["state"] == "open" and not job["duplicate_of"] and match.get("priority"))
                       or (filter == "review" and job["state"] != "closed" and (job["duplicate_of"] or match.get("review"))))
            if include and (not query or query in profile.key(" ".join([item["title"], item["company"], item["location"], *item["skills"]]))):
                results.append(job)
        return view(request, "jobs", jobs=results, filter=filter, q=q, counts=counts,
                    application_count=applications_count, alerts=alerts)

    @app.get("/jobs/new")
    def create_job(request: Request):
        return view(request, "new_job")

    @app.post("/jobs/new")
    async def save_job(request: Request):
        values = await form(request)
        data = {name: str(values.get(name, "")).strip() for name in (
            "title", "company", "url", "apply_url", "location", "country", "workplace", "remote_region",
            "description", "language", "deadline", "currency", "salary_basis", "salary_period",
        )}
        for name in ("salary_min", "salary_max"):
            if values.get(name):
                data[name] = values[name]
        if not data["description"]:
            raise Problem("İlan metnini girin.")
        data["description"] = data["description"][:40000]
        questions = [line.strip() for line in str(values.get("questions", "")).splitlines() if line.strip()][:25]
        data["questions"] = [{"name": f"manual_{i}", "label": label[:300], "required": False}
                             for i, label in enumerate(questions, 1)]
        with store.connect(write=True) as conn:
            job_id = catalog.import_manual(conn, data)
            matching.refresh(conn)
        return go(f"/jobs/{job_id}", request, "İlan kaydedildi.")

    @app.get("/jobs/{job_id}")
    def detail(request: Request, job_id: int):
        from .submission import route_for
        with store.connect() as conn:
            job = next((j for j in matching.ranked(conn) if j["id"] == job_id), None)
            if not job:
                raise Problem("İlan bulunamadı.")
            record = conn.execute("SELECT id,state FROM applications WHERE opportunity_id=?", (job_id,)).fetchone()
            versions = conn.execute("SELECT id,created FROM posting_versions WHERE posting_id=? ORDER BY id DESC",
                                    (job["posting_id"],)).fetchall()
            try:
                proposed_route = route_for(store, conn, job)
            except Problem:
                proposed_route = "manual"
        return view(request, "job", job=job, record=record, versions=versions, proposed_route=proposed_route,
                    locked=record and record["state"] in applications.BUSY_OR_FINAL)

    @app.get("/jobs/{job_id}/versions/{version_id}")
    def posting_version(request: Request, job_id: int, version_id: int):
        with store.connect() as conn:
            row = conn.execute("""SELECT v.*,p.source_id FROM posting_versions v JOIN postings p ON p.id=v.posting_id
                                WHERE p.opportunity_id=? AND v.id=?""", (job_id, version_id)).fetchone()
            if not row:
                raise Problem("İlan sürümü bulunamadı.")
        return view(request, "posting_version", job_id=job_id, version=row, item=json.loads(row["data"]))

    @app.post("/jobs/{job_id}/duplicate")
    async def duplicate(request: Request, job_id: int):
        values = await form(request)
        action = values.get("decision")
        with store.connect(write=True) as conn:
            row = conn.execute("SELECT duplicate_of FROM opportunities WHERE id=?", (job_id,)).fetchone()
            if not row or not row[0]:
                raise Problem("Kopya incelemesi bulunamadı.")
            original = row[0]
            if action == "same":
                if conn.execute("SELECT 1 FROM applications WHERE opportunity_id=?", (job_id,)).fetchone():
                    raise Problem("Başvuru geçmişi olan kayıt elle uzlaştırılmalı.")
                if conn.execute("SELECT duplicate_of FROM opportunities WHERE id=?", (original,)).fetchone()[0]:
                    raise Problem("Önce ilk kaydın kopya incelemesini tamamlayın.")
                conn.execute("UPDATE postings SET opportunity_id=? WHERE opportunity_id=?", (original, job_id))
                conn.execute("DELETE FROM matches WHERE opportunity_id=?", (job_id,))
                conn.execute("UPDATE opportunities SET duplicate_of=? WHERE duplicate_of=?", (original, job_id))
                conn.execute("DELETE FROM opportunities WHERE id=?", (job_id,))
                job_id = original
            elif action == "separate":
                conn.execute("UPDATE opportunities SET duplicate_of=NULL WHERE id=?", (job_id,))
            else:
                raise Problem("İnceleme sonucunu seçin.")
            matching.refresh(conn)
        return go(f"/jobs/{job_id}", request, "Kopya incelemesi kaydedildi.")

    @app.post("/jobs/{job_id}/prepare")
    async def prepare(request: Request, job_id: int):
        values = await form(request)
        out = await run_in_threadpool(applications.prepare, store, job_id, manual=values.get("route") == "manual")
        return go(f"/applications/{out}", request, "Başvuru paketi hazır.")

    @app.post("/jobs/{job_id}/prior")
    async def prior(request: Request, job_id: int):
        values = await form(request)
        with store.connect(write=True) as conn:
            app_id = applications.record_prior(conn, job_id, str(values.get("note", "")))
        return go(f"/applications/{app_id}", request, "Önceki başvuru kaydedildi.")

    @app.get("/applications")
    def app_list(request: Request):
        with store.connect() as conn:
            jobs = catalog.job_rows(conn)
            items = [dict(row, job=jobs[row["opportunity_id"]]["item"]) for row in
                     conn.execute("SELECT * FROM applications ORDER BY updated DESC")]
        return view(request, "applications", items=items)

    @app.get("/applications/{app_id}")
    def app_detail(request: Request, app_id: int, version: int | None = None):
        with store.connect() as conn:
            record, bundle, job = applications.get_app(conn, app_id)
            versions = conn.execute("SELECT id,created FROM bundles WHERE application_id=? ORDER BY id DESC", (app_id,)).fetchall()
            archived = version is not None and (not bundle or bundle["id"] != version)
            if archived:
                row = conn.execute("SELECT * FROM bundles WHERE application_id=? AND id=?", (app_id, version)).fetchone()
                if not row:
                    raise Problem("Paket sürümü bulunamadı.")
                bundle = dict(row)
            payload = json.loads(bundle["payload"]) if bundle else None
            events = conn.execute("SELECT * FROM application_events WHERE application_id=? ORDER BY id DESC", (app_id,)).fetchall()
            manual_approval = bundle and conn.execute("""SELECT 1 FROM approvals WHERE bundle_id=? AND route='manual'
                                                AND expires>? AND revoked IS NULL""", (bundle["id"], stamp())).fetchone()
            stale = bundle and (bundle["profile_version_id"] != profile.load_profile(conn)["id"]
                                or bundle["posting_version_id"] != job["version_id"] or job["state"] != "open"
                                or job["duplicate_of"])
        return view(request, "application", record=record, bundle=bundle, payload=payload, events=events,
                    job=job, stale=stale, locked=archived or record["state"] in applications.BUSY_OR_FINAL,
                    manual_approval=manual_approval, versions=versions, archived=archived,
                    follow=sorted(applications.FOLLOW_UP.get(record["state"], set())))

    @app.post("/applications/{app_id}/edit")
    async def app_edit(request: Request, app_id: int):
        values = await form(request)
        applications.edit_bundle(store, app_id, int(values.get("bundle", 0)), str(values.get("cover", "")),
                                 [str(answer) for answer in values.getlist("answer")],
                                 {key[8:]: value for key, value in values.items() if key.startswith("consent_")})
        return go(f"/applications/{app_id}", request, "Yeni paket sürümü kaydedildi.")

    @app.post("/applications/{app_id}/approve")
    async def app_approve(request: Request, app_id: int):
        values = await form(request)
        if values.get("confirm") != "yes":
            raise Problem("Gönderilecek bilgileri inceleyip onayı işaretleyin.")
        applications.approve(store, app_id, int(values.get("bundle", 0)), str(values.get("digest", "")))
        return go(f"/applications/{app_id}", request, "Paket onaylandı.")

    @app.post("/applications/{app_id}/record-result")
    async def app_result(request: Request, app_id: int):
        values = await form(request)
        with store.connect(write=True) as conn:
            applications.record_result(conn, app_id, values.get("state"), str(values.get("note", "")))
        return go(f"/applications/{app_id}", request, "Başvuru sonucu kaydedildi.")

    @app.get("/applications/{app_id}/documents/{name}")
    def download(app_id: int, name: str, bundle: int):
        types = {"cv.pdf": "application/pdf", "cv.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "on-yazi.txt": "text/plain", "yanitlar.txt": "text/plain"}
        with store.connect() as conn:
            row = conn.execute("""SELECT d.digest FROM documents d JOIN bundles b ON b.id=d.bundle_id
                WHERE b.application_id=? AND b.id=? AND d.name=?""", (app_id, bundle, name)).fetchone()
            if not row or name not in types:
                raise Problem("Belge bulunamadı.")
            content = store.get(row[0])
        return Response(content, media_type=types[name], headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/profile")
    def profile_view(request: Request):
        with store.connect() as conn:
            current = profile.load_profile(conn)
        return view(request, "profile", profile=current)

    @app.post("/profile/import")
    async def profile_import(request: Request):
        values = await form(request)
        upload = values.get("cv")
        if not getattr(upload, "filename", ""):
            raise Problem("Bir CV dosyası seçin.")
        additions = profile.import_facts(upload.filename, await upload.read(8 * 1024 * 1024 + 1))
        with store.connect(write=True) as conn:
            current = profile.load_profile(conn)
            profile.save_profile(conn, current["identity"], current["facts"] + additions, current["preferences"])
        return go("/profile", request, f"{len(additions)} taslak olgu eklendi.")

    @app.post("/profile")
    async def profile_save(request: Request):
        values = await form(request)
        with store.connect(write=True) as conn:
            current = profile.load_profile(conn)
            if str(current["id"] or "") != values.get("version"):
                raise Problem("Profil değişti. En son sürümü açın.")
            identity = {name: str(values.get(name, "")).strip() for name in ("name", "email", "phone")}
            facts = []
            for old in current["facts"]:
                suffix = old["id"]
                if values.get(f"remove{suffix}"):
                    continue
                fields = {name: values.get(f"{name}{suffix}") or None for name in ("kind", "text", "source", "start", "end", "language")}
                prior = old["verified_at"] if all(old[name] == value for name, value in fields.items()) else None
                verified = (prior or stamp()) if values.get(f"verify{suffix}") else None
                facts.append(dict(fields, id=suffix, verified_at=verified))
            if str(values.get("new_text", "")).strip():
                facts.append({"kind": values.get("new_kind"), "text": values.get("new_text"),
                              "source": values.get("new_source"), "language": values.get("new_language") or "und",
                              "start": values.get("new_start") or None, "end": values.get("new_end") or None,
                              "verified_at": stamp() if values.get("new_verify") else None})
            preferences = []
            indices = list(range(len(current["preferences"]))) + ["new"]
            for i in indices:
                value = str(values.get(f"pref_value_{i}", "")).strip()
                if value:
                    preferences.append({"kind": values.get(f"pref_kind_{i}"), "value": value,
                                        "required": bool(values.get(f"pref_required_{i}"))})
            profile.save_profile(conn, identity, facts, preferences)
            matching.refresh(conn)
        return go("/profile", request, "Profil sürümü kaydedildi.")

    @app.get("/sources")
    def sources(request: Request):
        error = ""
        try:
            configurations = settings.sources()
        except Problem as exc:
            configurations, error = [], str(exc)
        with store.connect() as conn:
            statuses = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM sources")}
            runs = conn.execute("SELECT * FROM source_runs ORDER BY id DESC LIMIT 20").fetchall()
            queue = conn.execute("SELECT kind,state,available,error FROM tasks WHERE state<>'done' ORDER BY id DESC LIMIT 20").fetchall()
            restore_lock = conn.execute("SELECT value FROM control WHERE key='restore_lock'").fetchone()
            unknown_count = conn.execute("SELECT COUNT(*) FROM applications WHERE state='submission_unknown'").fetchone()[0]
        return view(request, "sources", configurations=configurations, statuses=statuses, runs=runs, queue=queue,
                    error=error, restore_lock=restore_lock, unknown_count=unknown_count)

    @app.post("/sources/scan")
    async def scan(request: Request):
        values = await form(request)
        # Scheduling respects the configured source interval. The worker owns network I/O.
        count = schedule(store, only=values.get("source") or None)
        return go("/sources", request, "Tarama sıraya alındı." if count else "Kaynaklar güncel veya tarama sırada.")

    @app.get("/notifications")
    def notifications(request: Request):
        with store.connect() as conn:
            notices = conn.execute("SELECT * FROM notifications ORDER BY seen IS NOT NULL, id DESC LIMIT 100").fetchall()
        return view(request, "notifications", notices=notices)

    @app.post("/notifications/read")
    async def read_notifications(request: Request):
        await form(request)
        with store.connect(write=True) as conn:
            conn.execute("UPDATE notifications SET seen=? WHERE seen IS NULL", (stamp(),))
        return go("/notifications")

    return app
