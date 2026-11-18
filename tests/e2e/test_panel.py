import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from pypdf import PdfReader

from isakisi.queue import worker
from isakisi.settings import Source
from isakisi.submission import browser_prefill
from isakisi.web import create_app, login_token

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def browser():
    with playwright.sync_playwright() as runner:
        executable = Path(runner.chromium.executable_path)
        if not executable.exists():
            pytest.skip("Chromium için playwright install chromium çalıştırın.")
        browser = runner.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def panel(seeded):
    with socket.socket() as server_socket:
        server_socket.bind(("127.0.0.1", 0))
        port = server_socket.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(seeded), log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [server_socket]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(.05)
            assert server.started
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=8)


def test_browser_approval_uses_only_local_ats(browser, panel, seeded, tmp_path):
    screenshot_dir = Path(os.environ["ISAKISI_SCREENSHOTS"]) if os.getenv("ISAKISI_SCREENSHOTS") else None
    if screenshot_dir:
        screenshot_dir.mkdir(exist_ok=True, parents=True)
    context = browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    outgoing = []

    def intercept(route):
        if not route.request.url.startswith(panel + "/"):
            outgoing.append(route.request.url)
            route.abort()
        else:
            route.continue_()

    context.route("**/*", intercept)
    page.goto(panel)
    page.get_by_label("Giriş kodu").fill(login_token(seeded.session_key()))
    page.get_by_role("button", name="Giriş yap").click()
    playwright.expect(page.get_by_role("heading", name="İş ilanları")).to_be_visible()
    if screenshot_dir:
        page.screenshot(path=screenshot_dir / "ilanlar-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
    if screenshot_dir:
        page.screenshot(path=screenshot_dir / "ilanlar-mobile.png", full_page=True)
    page.get_by_role("link", name="Python Backend Geliştirici").click()
    page.get_by_role("button", name="Başvuru paketi hazırla").click()
    playwright.expect(page.get_by_role("heading", name="Başvuru paketi")).to_be_visible()
    with page.expect_download() as download_info:
        page.get_by_role("link", name="CV PDF").click()
    pdf = tmp_path / "cv.pdf"
    download_info.value.save_as(pdf)
    assert "Deniz Öztürk" in PdfReader(pdf).pages[0].extract_text()
    page.get_by_role("checkbox", name="hedefi, belgeleri", exact=False).check()
    page.get_by_role("button", name="Onayla ve simüle et").click()
    playwright.expect(page.get_by_text("Onaylandı, sırada", exact=True).first).to_be_visible()
    worker(seeded, once=True)
    page.reload()
    playwright.expect(page.get_by_text("Simülasyon tamamlandı", exact=True).first).to_be_visible()
    with seeded.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM simulated_receipts").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM application_events WHERE state='submitted'").fetchone()
    assert not outgoing


def test_prefill_leaves_submit_to_user(browser, seeded):
    url = "https://forms.example.com/apply"
    source = Source(id="portal", platform="jsonld", url=url, hosts=["forms.example.com"],
                    permission_url="https://forms.example.com/permission", checked_on="2026-11-18",
                    prefill=True, prefill_fields={"name": "Full name", "email": "Email", "resume": "Resume"})
    posts = []
    context = browser.new_context()

    def local_routes(pattern, guard):
        def handle(route):
            if route.request.url == url and route.request.method == "GET":
                route.fulfill(status=200, content_type="text/html", body="""
                  <form method="post">
                    <label>Full name <input name="name" required></label>
                    <label>Email <input type="email" name="email" required></label>
                    <label>Resume <input type="file" name="resume"></label>
                    <button>Submit</button>
                  </form>""")
            elif route.request.url == url:
                posts.append(route.request.method)
                route.fulfill(status=201)
            else:
                guard(route)
        context.route(pattern, handle)

    def inspect():
        page = context.pages[0]
        assert page.get_by_label("Full name").input_value() == "Deniz Öztürk"
        assert page.get_by_label("Email").input_value() == "deniz@example.invalid"
        assert page.locator("input[type=file]").evaluate("(input) => input.files[0].name") == "CV.pdf"
        context.close()

    @contextmanager
    def runner():
        ctx = SimpleNamespace(route=local_routes, route_web_socket=context.route_web_socket, new_page=context.new_page)
        controlled = SimpleNamespace(new_context=lambda **kwargs: ctx, close=inspect)
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: controlled))

    payload = {"target": url, "identity": {"name": "Deniz Öztürk", "email": "deniz@example.invalid", "phone": ""}}
    state, _ = browser_prefill(seeded, source, payload, b"%PDF-1.7", playwright=runner, handoff_seconds=0)
    assert state == "partial" and not posts
