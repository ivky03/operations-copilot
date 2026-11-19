"""End-to-end: a real browser signs in through the built web app.

Starts the API (serving the built React app) on a free local port against the test database,
then drives Chrome with Playwright the way a person would. Skipped automatically if the
frontend hasn't been built or Chrome isn't installed.
"""

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from app.cli import publish_reference, seed_reference

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html"
OPS = "ops.employee@brindlewood.example"
FIN = "finance.employee@brindlewood.example"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not DIST.exists(), reason="frontend not built (npm run build)"),
]
playwright_api = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_app(db):
    passwords = seed_reference(db)
    publish_reference(db)
    from app.main import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/", passwords
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def page():
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # Chrome not installed on this machine
            pytest.skip(f"Chrome not available: {e}")
        yield browser.new_page()
        browser.close()


def sign_in(page, email: str, password: str) -> None:
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()


def test_sign_in_shows_only_the_sources_your_groups_can_read(live_app, page):
    url, passwords = live_app
    page.goto(url)

    sign_in(page, OPS, "wrong-password-123")
    assert page.get_by_role("alert").inner_text() == "Invalid email or password."

    sign_in(page, OPS, passwords[OPS])
    page.get_by_text("Welcome, Olivia").wait_for()
    page.get_by_role("table").first.wait_for()
    assert page.locator("tbody tr").count() == 13
    assert page.get_by_text("shipping-and-delivery").count() == 1
    assert page.get_by_text("finance-margin-targets").count() == 0

    page.reload()  # the server-side session survives a refresh
    page.get_by_text("Welcome, Olivia").wait_for()

    page.get_by_role("button", name="Sign out").click()
    page.get_by_label("Email").wait_for()

    sign_in(page, FIN, passwords[FIN])
    page.get_by_text("Welcome, Felix").wait_for()
    page.get_by_role("table").first.wait_for()
    assert page.locator("tbody tr").count() == 12
    assert page.get_by_text("finance-margin-targets").count() == 1
    assert page.get_by_text("shipping-and-delivery").count() == 0
