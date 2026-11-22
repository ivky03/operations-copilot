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


def open_sources(page) -> None:
    """The source list is in a collapsible section below the question box."""
    page.get_by_text("Sources you can access").click()
    page.get_by_role("table").first.wait_for()


def test_sign_in_shows_only_the_sources_your_groups_can_read(live_app, page):
    url, passwords = live_app
    page.goto(url)

    sign_in(page, OPS, "wrong-password-123")
    assert page.get_by_role("alert").inner_text() == "Invalid email or password."

    sign_in(page, OPS, passwords[OPS])
    page.get_by_text("Welcome, Olivia").wait_for()
    open_sources(page)
    assert page.locator(".sources-panel tbody tr").count() == 13
    assert page.get_by_text("shipping-and-delivery").count() == 1
    assert page.get_by_text("finance-margin-targets").count() == 0

    page.reload()  # the server-side session survives a refresh
    page.get_by_text("Welcome, Olivia").wait_for()

    page.get_by_role("button", name="Sign out").click()
    page.get_by_label("Email").wait_for()

    sign_in(page, FIN, passwords[FIN])
    page.get_by_text("Welcome, Felix").wait_for()
    open_sources(page)
    assert page.locator(".sources-panel tbody tr").count() == 12
    assert page.get_by_text("finance-margin-targets").count() == 1
    assert page.get_by_text("shipping-and-delivery").count() == 0


def test_asking_a_question_shows_a_verified_answer_with_evidence(live_app, page):
    """Scripted model (no quota used); real browser, real API, real SQL."""
    from app.api.ask import get_planner_llm, get_writer_llm
    from app.main import app
    from tests.test_answer_pipeline import IRELAND_MARCH, fake

    planner, _ = fake(IRELAND_MARCH)
    writer, _ = fake({"answer": "Ireland's net sales in March 2026 were **−€509.00** [D1]."})
    app.dependency_overrides[get_planner_llm] = lambda: planner
    app.dependency_overrides[get_writer_llm] = lambda: writer
    try:
        url, passwords = live_app
        page.goto(url)
        sign_in(page, OPS, passwords[OPS])
        page.get_by_label("Ask a question about your data or policies").fill(
            "Ireland net sales in March 2026?"
        )
        page.get_by_role("button", name="Ask", exact=True).click()
        page.get_by_text("Answered · numbers and citations verified").wait_for()
        assert "−€509.00" in page.locator(".markdown").inner_text()
        evidence = page.locator(".evidence").first
        assert "Net sales" in evidence.inner_text()
        assert "transactions v1" in evidence.inner_text()
        evidence.get_by_text("Show SQL").click()
        assert "WITH c0 AS" in evidence.locator("pre.sql").inner_text()
    finally:
        app.dependency_overrides.clear()


def test_document_answer_shows_cited_passage_cards(live_app, page):
    """Scripted model; real browser, real API, real permission-filtered document search."""
    from app.api.ask import get_planner_llm, get_writer_llm
    from app.main import app
    from tests.test_answer_pipeline import fake

    planner, _ = fake({"kind": "documents", "doc_searches": ["return window"]})
    writer, _ = fake({"answer": "Customers have **30 days** to return unused products [C1]."})
    app.dependency_overrides[get_planner_llm] = lambda: planner
    app.dependency_overrides[get_writer_llm] = lambda: writer
    try:
        url, passwords = live_app
        page.goto(url)
        sign_in(page, OPS, passwords[OPS])
        page.get_by_label("Ask a question about your data or policies").fill("Return window?")
        page.get_by_role("button", name="Ask", exact=True).click()
        page.get_by_text("Answered · numbers and citations verified").wait_for()
        cited = page.locator(".passage.cited")
        assert cited.count() == 1
        text = cited.inner_text()
        assert "Return window" in text and "returns-refund-policy v2" in text
        assert "current" in text and "cited in the answer" in text
        assert page.locator(".passage.uncited").count() >= 1  # retrieved, shown dimmed
    finally:
        app.dependency_overrides.clear()


def test_conversation_follow_up_reopen_rename_delete(live_app, page):
    """A whole chat in a real browser: ask, follow up, start another, reopen, rename, delete."""
    from app.api.ask import get_planner_llm, get_writer_llm
    from app.main import app
    from tests.test_answer_pipeline import IRELAND_MARCH, fake

    april = {
        **IRELAND_MARCH,
        "standalone_question": "Ireland net sales in April 2026",
        "queries": [
            {
                **IRELAND_MARCH["queries"][0],
                "time_range": {"start": "2026-04-01", "end": "2026-05-01"},
            }
        ],
    }
    planner, _ = fake(IRELAND_MARCH, april, {"kind": "clarify", "clarification": "Which metric?"})
    writer, _ = fake({"answer": "March: −€509.00 [D1]."}, {"answer": "No April data [D1]."})
    app.dependency_overrides[get_planner_llm] = lambda: planner
    app.dependency_overrides[get_writer_llm] = lambda: writer
    page.on("dialog", lambda d: d.accept())  # confirm the delete
    try:
        url, passwords = live_app
        page.goto(url)
        sign_in(page, OPS, passwords[OPS])
        box = page.get_by_label("Ask a question about your data or policies")
        box.fill("Ireland net sales in March 2026?")
        box.press("Enter")
        page.get_by_text("March: −€509.00").wait_for()
        box.fill("And April?")
        box.press("Enter")
        page.get_by_text("No April data").wait_for()
        assert page.locator(".turn").count() == 2
        assert page.locator(".understood").inner_text() == (
            "Understood as: “Ireland net sales in April 2026”"
        )
        assert (
            page.locator(".turn").first.locator(".evidence-details").get_attribute("open") is None
        )

        sidebar = page.locator(".sidebar")
        sidebar.get_by_text("Ireland net sales in March 2026?").wait_for()
        sidebar.get_by_role("button", name="+ New chat").click()
        assert page.locator(".turn").count() == 0
        box.fill("Something vague")
        box.press("Enter")
        page.get_by_text("Which metric?").wait_for()
        assert sidebar.locator("li").count() == 2

        sidebar.get_by_text("Ireland net sales in March 2026?").click()  # reopen the first chat
        page.get_by_text("No April data").wait_for()
        assert page.locator(".turn").count() == 2

        sidebar.get_by_role("button", name="Rename “Ireland net sales in March 2026?”").click()
        sidebar.get_by_label("Conversation title").fill("Ireland")
        sidebar.get_by_label("Conversation title").press("Enter")
        sidebar.get_by_role("button", name="Ireland", exact=True).wait_for()

        sidebar.get_by_role("button", name="Delete “Ireland”").click()
        page.wait_for_function("document.querySelectorAll('.sidebar li').length === 1")
        assert page.locator(".turn").count() == 0  # the open chat was the deleted one
    finally:
        app.dependency_overrides.clear()


def test_progress_and_data_show_while_the_answer_is_still_being_written(live_app, page):
    """Proves the stream is incremental in a real browser (not buffered until the end)."""
    import time as _time

    from app.api.ask import get_planner_llm, get_writer_llm
    from app.llm.client import LLMClient
    from app.main import app
    from tests.test_answer_pipeline import IRELAND_MARCH, FakeProvider, fake

    class SlowProvider(FakeProvider):
        def complete(self, messages, timeout):
            _time.sleep(2.5)  # a slow model: the page must not sit blank meanwhile
            return super().complete(messages, timeout)

    planner, _ = fake(IRELAND_MARCH)
    writer = LLMClient([SlowProvider([{"answer": "Ireland: −€509.00 [D1]."}])], retry_delay=0)
    app.dependency_overrides[get_planner_llm] = lambda: planner
    app.dependency_overrides[get_writer_llm] = lambda: writer
    try:
        url, passwords = live_app
        page.goto(url)
        sign_in(page, OPS, passwords[OPS])
        box = page.get_by_label("Ask a question about your data or policies")
        box.fill("Ireland net sales in March 2026?")
        box.press("Enter")
        page.locator(".steps li.active", has_text="Writing the answer").wait_for(timeout=2000)
        assert page.locator(".steps li.done", has_text="Running queries").count() == 1
        assert "−€509.00" in page.locator(".evidence").first.inner_text()  # data already shown
        assert page.locator(".status").count() == 0  # but no answer yet
        page.get_by_text("Answered · numbers and citations verified").wait_for(timeout=10_000)
        assert page.locator(".steps").count() == 0
    finally:
        app.dependency_overrides.clear()


STEWARD = "data.steward@brindlewood.example"
SAMPLES = Path(__file__).resolve().parents[2] / "data" / "reference"


@pytest.fixture
def worker_thread(ingest_queues):
    """The ingestion worker, running in the background as it would in its own process."""
    from app.worker import Worker

    stop = threading.Event()

    def loop():
        worker = Worker()
        while not stop.is_set():
            worker.run_once(wait_seconds=1)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(timeout=10)


def test_a_steward_uploads_reviews_and_publishes_a_new_version(worker_thread, live_app, page):
    """Real browser, API, S3/SQS (Floci) and worker: upload → background check → preview →
    publish, plus a refused publish and a rejected file."""
    url, passwords = live_app
    page.goto(url)
    sign_in(page, STEWARD, passwords[STEWARD])
    page.get_by_role("button", name="Manage sources").click()
    page.get_by_role("button", name="Stores and sales channels").click()

    page.get_by_label("Manifest (YAML)").set_input_files(SAMPLES / "manifests" / "stores.yaml")
    page.get_by_label("Rows (CSV)").set_input_files(
        SAMPLES / "sample_uploads" / "stores_with_lisbon.csv"
    )
    page.get_by_role("button", name="Upload new version").click()
    page.get_by_role("button", name="Publish v2").wait_for(timeout=20000)
    preview = page.get_by_label("Version 2")
    assert "8 → 9 rows" in preview.inner_text()
    assert preview.get_by_role("cell", name="Lisbon Store").count() == 1
    page.get_by_role("button", name="Publish v2").click()
    preview.get_by_text("Published", exact=True).wait_for()

    # Leaving out a store that published transactions use: preparing works, publishing doesn't.
    page.get_by_label("Manifest (YAML)").set_input_files(SAMPLES / "manifests" / "stores.yaml")
    page.get_by_label("Rows (CSV)").set_input_files(
        SAMPLES / "sample_uploads" / "stores_without_london.csv"
    )
    page.get_by_role("button", name="Upload new version").click()
    page.get_by_role("button", name="Publish v3").click(timeout=20000)
    page.get_by_text("Can't publish:").wait_for()
    assert "uses store_id 'S01'" in page.get_by_role("alert").inner_text()

    page.get_by_role("button", name="Sales transactions").click()
    page.get_by_label("Manifest (YAML)").set_input_files(
        SAMPLES / "manifests" / "transactions.yaml"
    )
    page.get_by_label("Rows (CSV)").set_input_files(
        SAMPLES / "invalid_uploads" / "transactions_with_errors.csv"
    )
    page.get_by_role("button", name="Upload new version").click()
    page.get_by_text("This upload was rejected:").wait_for(timeout=20000)
    assert "row 3, column unit_price" in page.get_by_role("alert").inner_text()


ADMIN = "admin@brindlewood.example"


def test_an_admin_registers_a_new_dataset_and_a_steward_publishes_it(worker_thread, live_app, page):
    """The whole path for a table the application has never seen: registered in the browser,
    uploaded, prepared by the worker, published, and visible to the granted group only."""
    url, passwords = live_app
    stage_b = SAMPLES.parent / "stage_b"
    page.goto(url)
    sign_in(page, ADMIN, passwords[ADMIN])
    page.get_by_role("button", name="Sources", exact=True).click()
    page.get_by_label("Name").fill("purchase-orders")
    assert "underscores" in page.locator("#name-hint").inner_text()
    assert page.get_by_role("button", name="Register").is_disabled()
    page.get_by_label("Name").fill("suppliers")
    page.get_by_label("Title").fill("Suppliers")
    page.get_by_label("operations").check()
    page.get_by_role("button", name="Register").click()
    page.get_by_text("Registered suppliers").wait_for()
    page.get_by_role("button", name="Sign out").click()

    sign_in(page, STEWARD, passwords[STEWARD])
    page.get_by_role("button", name="Manage sources").click()
    page.get_by_role("button", name="Suppliers").click()
    page.get_by_label("Manifest (YAML)").set_input_files(stage_b / "manifests" / "suppliers.yaml")
    page.get_by_label("Rows (CSV)").set_input_files(stage_b / "tables" / "suppliers.csv")
    page.get_by_role("button", name="Upload new version").click()
    page.get_by_role("button", name="Publish v1").click(timeout=20000)
    page.get_by_label("Version 1").get_by_text("Published", exact=True).wait_for()
    page.get_by_role("button", name="Sign out").click()

    sign_in(page, OPS, passwords[OPS])
    page.get_by_text("Welcome, Olivia").wait_for()
    open_sources(page)
    assert page.get_by_text("suppliers", exact=True).count() == 1
    page.get_by_role("button", name="Sign out").click()

    sign_in(page, FIN, passwords[FIN])  # not granted to Finance
    page.get_by_text("Welcome, Felix").wait_for()
    open_sources(page)
    assert page.get_by_text("suppliers", exact=True).count() == 0
