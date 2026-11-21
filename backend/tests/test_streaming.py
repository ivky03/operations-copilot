"""Streamed answers (M5b): progress and evidence early, the answer only after verification."""

import json

import pytest
from sqlalchemy import select

from app.api.ask import get_planner_llm, get_writer_llm
from app.cli import publish_reference, seed_reference
from app.main import app
from app.models import AnswerRun, User
from app.security import hash_password
from tests.conftest import PASSWORD, login
from tests.test_answer_pipeline import IRELAND_MARCH, fake

pytestmark = pytest.mark.integration

OPS = "ops.employee@brindlewood.example"


@pytest.fixture
def scripted(db):
    seed_reference(db)
    publish_reference(db)
    db.scalar(select(User).where(User.email == OPS)).password_hash = hash_password(PASSWORD)
    db.commit()

    def script(planner_replies, writer_replies):
        planner, _ = fake(*planner_replies)
        writer, _ = fake(*writer_replies)
        app.dependency_overrides[get_planner_llm] = lambda: planner
        app.dependency_overrides[get_writer_llm] = lambda: writer

    yield script
    app.dependency_overrides.clear()


def stream(client, csrf, body) -> list[tuple[str, dict]]:
    events, event = [], None
    with client.stream(
        "POST", "/api/ask/stream", json=body, headers={"X-CSRF-Token": csrf}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((event, json.loads(line.removeprefix("data: "))))
    return events


def test_steps_and_evidence_arrive_before_the_verified_answer(client, scripted, db):
    scripted([IRELAND_MARCH], [{"answer": "Ireland's net sales were -€509.00 [D1]."}])
    csrf = login(client, OPS)
    events = stream(client, csrf, {"question": "Ireland net sales in March 2026?"})
    names = [e for e, _ in events]
    assert names == [
        "step", "planned", "step", "evidence", "step", "step", "answer",
    ]  # fmt: skip
    assert [d["step"] for e, d in events if e == "step"] == [
        "planning", "querying", "writing", "verifying",
    ]  # fmt: skip
    evidence = dict(events)["evidence"]["items"][0]
    assert evidence["rows"] == [{"net_sales": -509.0}] and "definition" not in evidence
    answer = events[-1][1]
    assert answer["status"] == "answered" and "-€509.00" in answer["answer"]
    assert db.get(AnswerRun, answer["run_id"]).status == "answered"  # saved like /api/ask


def test_an_unverified_draft_never_appears_in_the_stream(client, scripted):
    scripted(
        [IRELAND_MARCH],
        [{"answer": "Ireland made €1,234.00 [D1]."}, {"answer": "Net sales were -€509.00 [D1]."}],
    )
    csrf = login(client, OPS)
    events = stream(client, csrf, {"question": "Ireland net sales in March 2026?"})
    assert "rewriting" in [d.get("step") for _, d in events]
    assert "1,234" not in json.dumps(events)  # rejected draft: kept in the trace only
    assert events[-1][1]["answer"] == "Net sales were -€509.00 [D1]."


def test_documents_stream_their_passages(client, scripted):
    scripted(
        [{"kind": "documents", "doc_searches": ["return window"]}],
        [{"answer": "x", "answerable": False}],
    )
    csrf = login(client, OPS)
    events = stream(client, csrf, {"question": "What is our return window?"})
    passages = dict(events)["passages"]["items"]
    assert passages and {"id", "source", "version", "current"} <= set(passages[0])
    assert events[-1][1]["status"] == "unanswerable"


def test_stream_continues_a_conversation(client, scripted):
    scripted([{"kind": "clarify", "clarification": "Which metric?"}, IRELAND_MARCH],
             [{"answer": "-€509.00 [D1]"}])  # fmt: skip
    csrf = login(client, OPS)
    first = stream(client, csrf, {"question": "How are we doing?"})[-1][1]
    cid = first["conversation_id"]
    second = stream(
        client, csrf, {"question": "Ireland net sales, March 2026", "conversation_id": cid}
    )
    assert second[-1][1]["conversation_id"] == cid
    turns = client.get(f"/api/conversations/{cid}").json()["turns"]
    assert [t["status"] for t in turns] == ["clarification", "answered"]


def test_stream_needs_login_and_csrf(client, scripted):
    assert client.post("/api/ask/stream", json={"question": "anything?"}).status_code == 401
    login(client, OPS)
    assert client.post("/api/ask/stream", json={"question": "anything?"}).status_code == 403


def test_a_crash_becomes_an_error_event_not_a_broken_stream(client, scripted, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("app.api.ask.answer_question", boom)
    scripted([], [])
    csrf = login(client, OPS)
    events = stream(client, csrf, {"question": "Anything at all?"})
    assert events == [("error", {"message": "Something went wrong. Please try again."})]
