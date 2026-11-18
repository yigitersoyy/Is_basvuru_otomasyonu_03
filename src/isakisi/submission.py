import json
import os
import re
import time

import httpx

from .catalog import canonical_url
from .db import digest, stamp
from .settings import Problem, allowed_url
from .sources import Reader, board_url

SIM_URL = "http://isakisi.local/sahte-ats"
STANDARD = {"first_name", "last_name", "email", "phone", "resume", "cover_letter"}


def source_for(settings, job):
    if job["platform"] in {"manual", "fixture"}:
        return None
    return next((s for s in settings.sources() if s.id == job["source_id"] and s.platform == job["platform"]), None)


def route_for(store, conn, job):
    settings = store.settings
    if conn.execute("SELECT 1 FROM control WHERE key='restore_lock'").fetchone():
        return "manual"
    if job["platform"] == "fixture":
        return "simulate" if settings.submit_mode == "simulate" else "manual"
    if settings.discovery_mode != "live" or settings.submit_mode != "authorized":
        return "manual"
    source = source_for(settings, job)
    if source and source.read:
        if source.submit and os.getenv(source.secret_env):
            return "authorized"
        if source.prefill:
            return "prefill"
    return "manual"


def api_target(source, job):
    if not re.fullmatch(r"[0-9]+", job["external_key"]):
        raise Problem("Greenhouse ilan kimliği doğrulanamadı.")
    return allowed_url(f"{board_url(source)}/{job['external_key']}", source.hosts)


def fetch_schema(source, job, client):
    target = api_target(source, job)
    try:
        data = Reader(source, client).json(target, {"questions": "true"})
        if str(data["id"]) != job["external_key"] or "questions" not in data or not isinstance(data["questions"], list):
            raise ValueError
        return {field: data.get(field) for field in (
            "questions", "location_questions", "compliance", "demographic_questions", "data_compliance", "education"
        )}
    except (ValueError, KeyError, TypeError):
        raise Problem("İşveren formu doğrulanamadı. Resmî formu kullanın.") from None


def supported_questions(schema):
    if any(schema.get(field) for field in (
        "location_questions", "compliance", "demographic_questions", "education"
    )):
        raise Problem("Bu form ek beyan istiyor. Resmî formu kullanın.")
    questions = []
    for question in schema["questions"]:
        fields = question.get("fields") or []
        if any(f.get("name") in STANDARD for f in fields):
            continue
        if len(fields) != 1 or fields[0].get("type") not in {"input_text", "textarea", "multi_value_single_select"}:
            if question.get("required") or fields:
                raise Problem("Bu soru türü için resmî formu kullanın.")
            continue
        field = fields[0]
        name = field.get("name", "")
        if not re.fullmatch(r"question_[0-9]+", name):
            raise Problem("Bu soru için resmî formu kullanın.")
        questions.append({"name": name, "label": question.get("label") or name,
                          "required": bool(question.get("required")), "values": field.get("values") or []})
    return questions


def consent_fields(schema):
    rules = schema.get("data_compliance") or []
    labels = {
        "gdpr_consent_given": "Aday verilerimin bu başvuru için işlenmesine izin veriyorum.",
        "gdpr_processing_consent_given": "Aday verilerimin bu başvuru için işlenmesine izin veriyorum.",
        "gdpr_retention_consent_given": "Aday verilerimin işverenin belirttiği süre boyunca saklanmasına izin veriyorum.",
    }
    result = {}
    for rule in rules:
        if rule.get("type") != "gdpr":
            raise Problem("Bu rıza türü için işverenin resmî formunu kullanın.")
        if rule.get("requires_processing_consent"):
            result["gdpr_processing_consent_given"] = labels["gdpr_processing_consent_given"]
        if rule.get("requires_retention_consent"):
            result["gdpr_retention_consent_given"] = labels["gdpr_retention_consent_given"]
        if rule.get("requires_consent") and not any(name in result for name in (
            "gdpr_processing_consent_given", "gdpr_retention_consent_given"
        )):
            result["gdpr_consent_given"] = labels["gdpr_consent_given"]
    return result


def preflight(store, job, payload, route, client):
    source = source_for(store.settings, job) if route in {"authorized", "prefill"} else None
    if route == "authorized":
        if not source or not source.submit or not os.getenv(source.secret_env):
            raise Problem("İşveren gönderim izni veya anahtarı bulunamadı.")
        if payload["target"] != api_target(source, job) or payload["schema"] != fetch_schema(source, job, client):
            raise Problem("İşveren formu değişti. Paketi yeniden hazırlayın.")
        validate_answers(payload, mandatory=True)
    if route == "prefill":
        if not source or not source.prefill or payload.get("prefill_fields") != source.prefill_fields:
            raise Problem("Form doldurma izni veya alanları değişti.")
        allowed_url(payload["target"], source.hosts)
    return source


def validate_answers(payload, mandatory=False):
    for question, answer in zip(payload["questions"], payload["answers"], strict=True):
        if mandatory and question.get("required") and not answer.strip():
            raise Problem("Zorunlu form yanıtlarını kaydedin.")
        choices = question.get("values") or []
        if answer and choices and answer not in {str(choice["value"]) for choice in choices}:
            raise Problem("Form yanıtı için listeden bir seçenek seçin.")
    for key in payload.get("consent_fields", {}):
        value = payload["consent"].get(key, "")
        if value not in {"", "yes", "no"} or (mandatory and not value):
            raise Problem("Paketin rıza seçeneklerini inceleyip tercihinizi kaydedin.")


def send_greenhouse(client, source, payload, resume):
    name = payload["identity"]["name"].strip().split(maxsplit=1)
    fields = {"first_name": name[0], "last_name": name[1], "email": payload["identity"]["email"]}
    if payload["identity"]["phone"]:
        fields["phone"] = payload["identity"]["phone"]
    fields.update({q["name"]: a for q, a in zip(payload["questions"], payload["answers"], strict=True) if a})
    fields.update({f"data_compliance[{k}]": "true" if v == "yes" else "false"
                   for k, v in payload["consent"].items() if v})
    result = client.post(payload["target"], auth=(os.environ[source.secret_env], ""), data=fields,
                         files={"resume": ("CV.pdf", resume, "application/pdf"),
                                "cover_letter": ("cover.txt", payload["cover"].encode(), "text/plain")},
                         follow_redirects=False)
    if result.status_code not in {200, 201}:
        return "submission_unknown", ""
    try:
        data = result.json()
    except ValueError:
        return "submission_unknown", ""
    if not isinstance(data, dict) or data.get("errors") or data.get("error"):
        return "submission_unknown", ""
    token = data.get("id") or data.get("application_id")
    if token is not None and re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", str(token)):
        return "submitted", f"Greenhouse · {token}"
    if data.get("success") is True or data.get("success") == "true":
        return "submitted", f"Greenhouse · HTTP {result.status_code}"
    return "submission_unknown", ""


def simulate(store, attempt, payload):
    def ats(request):
        if str(request.url) != SIM_URL or request.method != "POST":
            return httpx.Response(404)
        receipt = "SIM-" + attempt[:12].upper()
        with store.connect(write=True) as conn:
            conn.execute("INSERT INTO simulated_receipts VALUES (?,?,?)", (attempt, receipt, stamp()))
        return httpx.Response(201, json={"id": receipt})

    with httpx.Client(transport=httpx.MockTransport(ats)) as client:
        resp = client.post(SIM_URL, json={"attempt": attempt, "package": digest(payload)})
    return "simulated", resp.json()["id"]


def browser_prefill(store, source, payload, resume, *, playwright=None, handoff_seconds=180):
    if playwright is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise Problem("Formu doldurmak için browser eklentisini kurun.") from None
        playwright = sync_playwright
    target = allowed_url(payload["target"], source.hosts)
    identity = payload["identity"]
    name = identity["name"].split(maxsplit=1)
    values = {**identity, "first_name": name[0], "last_name": name[-1]}
    with playwright() as runner:
        browser = runner.chromium.launch(headless=handoff_seconds == 0)
        context = browser.new_context(service_workers="block", accept_downloads=False)
        try:
            def guard(route):
                try:
                    allowed_url(route.request.url, source.hosts)
                except Problem:
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", guard)
            context.route_web_socket("**/*", lambda ws: ws.close())
            page = context.new_page()
            page.set_default_timeout(4000)
            page.goto(target, wait_until="domcontentloaded", timeout=15000)
            allowed_url(page.url, source.hosts)
            if canonical_url(page.url) != canonical_url(target):
                return "partial", "Form adresi değişti. İlanı panelden kontrol edin."
            blocked = page.locator("iframe[src*='captcha'], input[type='password']").count()
            fields = {field: page.get_by_label(label, exact=True) for field, label in source.prefill_fields.items()}
            if not blocked and all(locator.count() == 1 for locator in fields.values()):
                for field, locator in fields.items():
                    allowed_url(page.url, source.hosts)
                    if canonical_url(page.url) != canonical_url(target):
                        return "partial", "Form adresi değişti. İlanı panelden kontrol edin."
                    if field == "resume":
                        locator.set_input_files({"name": "CV.pdf", "mimeType": "application/pdf", "buffer": resume})
                    elif values.get(field):
                        locator.fill(values[field])
                note = "İzinli alanlar dolduruldu. Gönderim sonucu kullanıcı teyidi bekliyor."
            else:
                note = "Form değişikliği veya doğrulama adımı kullanıcı işlemi bekliyor."
            if handoff_seconds:
                end = time.monotonic() + handoff_seconds
                while time.monotonic() < end and not page.is_closed():
                    page.wait_for_timeout(500)
            return "partial", note
        finally:
            browser.close()
