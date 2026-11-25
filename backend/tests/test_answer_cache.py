"""The answer cache (M7b): reused only when nothing the answer depends on has changed, never
across different access, and erased with a deleted source. Leakage through the cache is also
probed in test_leakage.py."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.answer import cache
from app.answer.pipeline import answer_question
from app.api.ask import get_planner_llm, get_writer_llm
from app.cli import publish_reference, seed_reference
from app.main import app
from app.models import AnswerCacheEntry, AnswerRun, Conversation, User
from tests.conftest import login
from tests.test_answer_pipeline import IRELAND_MARCH, fake
from tests.test_ingestion_jobs import upload_stores, work

pytestmark = pytest.mark.integration

ADMIN, STEWARD = "admin@example.com", "steward@example.com"
OPS, OPS2, FIN = "ops@example.com", "ops2@example.com", "fin@example.com"
QUESTION = "What were Ireland's net sales in March 2026?"
ANSWER = {"answer": "Ireland's net sales in March 2026 were -€509.00 [D1]."}
SHIPPING = {"kind": "documents", "doc_searches": ["standard delivery time"]}


@pytest.fixture
def setup(db, make_user, ingest_queues):
    seed_reference(db)
    publish_reference(db)
    make_user(ADMIN, role="admin")
    make_user(STEWARD, role="steward", groups=("operations", "finance"))
    make_user(OPS, groups=("operations",))
    make_user(OPS2, groups=("operations",))
    make_user(FIN, groups=("operations", "finance"))
    return db


def user(db, email) -> User:
    db.expire_all()
    return db.scalar(select(User).where(User.email == email))


def ask(db, email, question=QUESTION, plan=IRELAND_MARCH, answer=ANSWER, conversation=None):
    """Ask with the cache on. Returns the result and how many model calls it took."""
    planner, p = fake(plan, plan)
    writer, w = fake(answer, answer)
    result = answer_question(
        db, user(db, email), question, planner, writer, conversation, use_cache=True
    )
    return result, len(p.prompts) + len(w.prompts)


def entries(db) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(AnswerCacheEntry))


def as_user(client, email):
    client.cookies.clear()
    client.headers["X-CSRF-Token"] = login(client, email)
    return client


# ----------------------------------------------------------------------------- reuse


def test_the_same_question_with_the_same_access_is_reused_without_model_calls(setup):
    db = setup
    first, calls = ask(db, OPS)
    assert first.status == "answered" and not first.cached and calls == 2

    # Spelled differently, same question; a colleague in the same group can read the same.
    again, calls = ask(db, OPS, "  what were IRELAND's net sales in march 2026")
    colleague, colleague_calls = ask(db, OPS2)
    assert again.cached and colleague.cached and calls == colleague_calls == 0
    assert again.answer == first.answer and again.evidence == first.evidence
    assert again.sources == first.sources

    # Each reuse is saved as the asker's own run, pointing at the original.
    for result in (again, colleague):
        run = db.get(AnswerRun, result.run_id)
        assert str(run.cached_from) == first.run_id and run.question == result.question
    assert db.get(AnswerCacheEntry, _key(db, OPS)).hits == 2


def _key(db, email) -> str:
    planner, _ = fake()
    writer, _ = fake()
    readable = cache.readable_versions(db, user(db, email))
    return cache.cache_key(QUESTION, readable, planner, writer)


def test_people_who_can_read_different_sources_never_share_an_answer(setup):
    db = setup
    ask(db, FIN)  # Finance can also read Finance-only sources: a different key
    result, calls = ask(db, OPS)
    assert not result.cached and calls == 2
    assert entries(db) == 2


def test_a_new_version_of_anything_readable_means_a_fresh_answer(setup, client):
    db = setup
    ask(db, OPS)
    as_user(client, STEWARD)
    upload_stores(client)
    work()
    assert client.post("/api/steward/sources/stores/versions/2/publish").status_code == 200

    result, calls = ask(db, OPS)
    assert not result.cached and calls == 2
    assert ("stores", 2) in {(s["source"], s["version"]) for s in result.sources}


def test_losing_access_to_any_source_means_a_fresh_answer(setup, client):
    db = setup
    ask(db, OPS)
    as_user(client, ADMIN)
    body = {"groups": ["finance"]}  # an unrelated document, no longer for Operations
    assert (
        client.put("/api/admin/sources/shipping-and-delivery/grants", json=body).status_code == 200
    )
    result, calls = ask(db, OPS)
    assert not result.cached and calls == 2


def test_follow_ups_and_unverified_answers_are_not_cached(setup):
    db = setup
    chat = Conversation(user_id=user(db, OPS).id, title="t")
    db.add(chat)
    db.commit()
    ask(db, OPS, conversation=chat)  # first turn: cached (no history yet)
    assert entries(db) == 1
    ask(db, OPS, "And Germany?", conversation=chat)  # a follow-up: means something only here
    assert entries(db) == 1

    invented = {"answer": "It was €1,234,567 [D1]."}  # fails verification twice: a fallback
    result, _ = ask(db, OPS, "Ireland net sales March 2026, roughly?", answer=invented)
    assert result.status == "answered_fallback"
    assert entries(db) == 1


def test_an_expired_entry_is_not_used_and_is_swept(setup):
    db = setup
    ask(db, OPS)
    db.execute(update(AnswerCacheEntry).values(created_at=datetime.now(UTC) - timedelta(days=2)))
    db.commit()
    result, calls = ask(db, OPS)
    assert not result.cached and calls == 2
    assert cache.purge_expired(db) == 1


def test_deleting_a_source_erases_every_cached_copy_and_its_entry(setup, client):
    db = setup
    original, _ = ask(
        db, OPS, "How long does standard delivery take?", SHIPPING, {"answer": "See [C1]."}
    )
    copy, _ = ask(db, OPS2, "How long does standard delivery take?", SHIPPING)
    assert copy.cached and copy.passages
    as_user(client, ADMIN)
    resp = client.post(
        "/api/admin/sources/shipping-and-delivery/delete",
        json={"confirm": "shipping-and-delivery"},
    )
    assert resp.json()["answers_erased"] == 2
    db.expire_all()
    for result in (original, copy):
        run = db.get(AnswerRun, result.run_id)
        assert run.answer is None and run.passages == [] and run.redacted_reason
    assert entries(db) == 0


# ----------------------------------------------------------------------------- over the API


def test_the_api_says_when_an_answer_was_reused(setup, client):
    app.dependency_overrides[get_planner_llm] = lambda: fake(IRELAND_MARCH)[0]
    app.dependency_overrides[get_writer_llm] = lambda: fake(ANSWER)[0]
    try:
        as_user(client, OPS)
        first = client.post("/api/ask", json={"question": QUESTION}).json()
        second = client.post("/api/ask", json={"question": QUESTION}).json()
    finally:
        app.dependency_overrides.clear()
    assert first["cached"] is False and second["cached"] is True
    saved = client.get(f"/api/conversations/{second['conversation_id']}").json()
    assert saved["turns"][0]["cached"] is True
