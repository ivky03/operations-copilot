"""Conversations (M5a): follow-up questions, saved chats, privacy and revoked access.

Scripted fake models; real database, real API. Model quality on follow-ups is measured by the
answer evaluation's follow_up cases.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.answer.history import MAX_TURNS
from app.answer.pipeline import answer_question
from app.api.ask import get_planner_llm, get_writer_llm
from app.cli import publish_reference, seed_reference
from app.main import app
from app.models import AnswerRun, Conversation, LlmCall, Source, SourceGrant, User
from app.security import hash_password
from tests.conftest import PASSWORD, login
from tests.test_answer_pipeline import IRELAND_MARCH, fake

pytestmark = pytest.mark.integration

OPS = "ops.employee@brindlewood.example"
FINANCE = "finance.employee@brindlewood.example"
IRELAND_APRIL = {
    **IRELAND_MARCH,
    "standalone_question": "What were Ireland's net sales in April 2026?",
    "queries": [
        {
            **IRELAND_MARCH["queries"][0],
            "time_range": {"start": "2026-04-01", "end": "2026-05-01"},
        }
    ],
}


DESK_LAMPS = {
    "kind": "data",
    "queries": [
        {
            "metric": "units_sold",
            "filters": [{"dimension": "product_name", "values": ["desk lamp"]}],
        }
    ],
}


@pytest.fixture
def users(db):
    seed_reference(db)
    publish_reference(db)
    found = {
        "ops": db.scalar(select(User).where(User.email == OPS)),
        "finance": db.scalar(select(User).where(User.email == FINANCE)),
    }
    for u in found.values():
        u.password_hash = hash_password(PASSWORD)  # seeded passwords are random
    db.commit()
    return found


def turn(db, user, conversation, question, plans, answers):
    planner, p = fake(*plans)
    writer, w = fake(*answers)
    return answer_question(db, user, question, planner, writer, conversation), p, w


@pytest.fixture
def chat(db, users):
    conversation = Conversation(user_id=users["ops"].id, title="Ireland")
    db.add(conversation)
    db.commit()
    return conversation


# ----------------------------------------------------------------------------- follow-ups


def test_follow_up_sees_the_previous_turn_and_is_restated(db, users, chat):
    turn(db, users["ops"], chat, "Ireland net sales in March 2026?", [IRELAND_MARCH],
         [{"answer": "Ireland: -€509.00 [D1]."}])  # fmt: skip
    result, planner, writer = turn(
        db, users["ops"], chat, "And April?", [IRELAND_APRIL], [{"answer": "€0.00 [D1]"}]
    )
    prompt = planner.prompts[0][1]["content"]
    assert (
        "# Conversation so far" in prompt and 'Turn 1: "Ireland net sales in March 2026?"' in prompt
    )
    assert "looked up: net_sales; where country = Ireland; from 2026-03-01" in prompt
    assert prompt.rstrip().endswith("# New question\nAnd April?")
    # Every later step uses the restated question, not "And April?"
    assert "What were Ireland's net sales in April 2026?" in writer.prompts[0][1]["content"]
    assert result.question == "And April?"
    run = db.get(AnswerRun, result.run_id)
    assert run.conversation_id == chat.id
    assert run.plan["standalone_question"] == "What were Ireland's net sales in April 2026?"


def test_history_never_carries_data_rows_or_answer_numbers(db, users, chat):
    turn(db, users["ops"], chat, "Ireland net sales in March 2026?", [IRELAND_MARCH],
         [{"answer": "Ireland: -€509.00 [D1]."}])  # fmt: skip
    _, planner, _ = turn(
        db, users["ops"], chat, "And April?", [IRELAND_APRIL], [{"answer": "x [D1]"}]
    )
    assert "509" not in json.dumps(planner.prompts)


def test_a_single_question_has_no_history_section(db, users):
    planner, p = fake(IRELAND_MARCH)
    writer, _ = fake({"answer": "-€509.00 [D1]"})
    answer_question(db, users["ops"], "Ireland net sales in March 2026?", planner, writer)
    assert "Conversation so far" not in p.prompts[0][1]["content"]


def test_history_is_limited_to_the_last_turns(db, users, chat):
    for n in range(MAX_TURNS + 2):
        turn(db, users["ops"], chat, f"question number {n}?",
             [{"kind": "clarify", "clarification": "Which metric?"}], [])  # fmt: skip
    _, planner, _ = turn(db, users["ops"], chat, "final?", [{"kind": "clarify"}], [])
    prompt = planner.prompts[0][1]["content"]
    assert "question number 0?" not in prompt and f"question number {MAX_TURNS + 1}?" in prompt
    assert prompt.count("Turn ") == MAX_TURNS


def test_clarification_is_in_history_so_a_reply_can_be_resolved(db, users, chat):
    plan = {
        "kind": "data",
        "queries": [
            {
                "metric": "units_sold",
                "filters": [{"dimension": "product_name", "values": ["desk lamp"]}],
            }
        ],
    }
    first, _, _ = turn(db, users["ops"], chat, "How many desk lamps did we sell?", [plan], [])
    assert first.status == "clarification"
    _, planner, _ = turn(db, users["ops"], chat, "the blue one", [{"kind": "clarify"}], [])
    assert "we asked the user:" in planner.prompts[0][1]["content"]
    assert "Blue Desk Lamp" in planner.prompts[0][1]["content"]


def test_revoked_source_drops_out_of_follow_up_context(db, users, chat):
    turn(db, users["ops"], chat, "Ireland net sales in March 2026?", [IRELAND_MARCH],
         [{"answer": "-€509.00 [D1]"}])  # fmt: skip
    returns = db.scalar(select(Source).where(Source.slug == "returns"))  # net_sales uses it
    db.query(SourceGrant).filter(SourceGrant.source_id == returns.id).delete()
    db.commit()
    _, planner, _ = turn(db, users["ops"], chat, "And April?", [{"kind": "clarify"}], [])
    assert "Conversation so far" not in planner.prompts[0][1]["content"]


# ----------------------------------------------------------------------------- API


@pytest.fixture
def api(users):
    planner_replies: list = []
    writer_replies: list = []
    app.dependency_overrides[get_planner_llm] = lambda: fake(*planner_replies)[0]
    app.dependency_overrides[get_writer_llm] = lambda: fake(*writer_replies)[0]
    yield planner_replies, writer_replies
    app.dependency_overrides.clear()


def _ask(client, csrf, question, conversation_id=None):
    body = {"question": question} | (
        {"conversation_id": conversation_id} if conversation_id else {}
    )
    return client.post("/api/ask", json=body, headers={"X-CSRF-Token": csrf})


def test_ask_starts_a_conversation_and_follow_ups_join_it(client, api):
    plans, answers = api
    csrf = login(client, OPS)
    plans.append(IRELAND_MARCH)
    answers.append({"answer": "Ireland: -€509.00 [D1]."})
    first = _ask(client, csrf, "What were Ireland's net sales in March 2026?").json()
    cid = first["conversation_id"]
    plans[:] = [IRELAND_APRIL]
    answers[:] = [{"answer": "Nothing in April [D1]."}]
    second = _ask(client, csrf, "And April?", cid).json()
    assert second["conversation_id"] == cid

    listing = client.get("/api/conversations").json()
    assert [c["id"] for c in listing] == [cid]
    assert listing[0]["title"] == "What were Ireland's net sales in March 2026?"
    convo = client.get(f"/api/conversations/{cid}").json()
    assert [t["question"] for t in convo["turns"]] == [
        "What were Ireland's net sales in March 2026?",
        "And April?",
    ]
    assert convo["turns"][0]["evidence"][0]["rows"] == [{"net_sales": -509.0}]


def test_long_questions_make_short_titles(client, api):
    plans, _ = api
    plans.append({"kind": "clarify", "clarification": "Which?"})
    csrf = login(client, OPS)
    cid = _ask(client, csrf, "word " * 60).json()["conversation_id"]
    title = client.get(f"/api/conversations/{cid}").json()["title"]
    assert len(title) <= 80 and title.endswith("…")


def test_clarification_options_survive_reopening(client, api):
    plans, _ = api
    plans.append(DESK_LAMPS)
    csrf = login(client, OPS)
    cid = _ask(client, csrf, "How many desk lamps did we sell?").json()["conversation_id"]
    turn0 = client.get(f"/api/conversations/{cid}").json()["turns"][0]
    assert turn0["status"] == "clarification" and "Blue Desk Lamp" in turn0["options"]


def test_conversations_are_private(client, api, users):
    plans, _ = api
    plans.append({"kind": "clarify", "clarification": "Which?"})
    csrf = login(client, OPS)
    cid = _ask(client, csrf, "Something vague?").json()["conversation_id"]

    other = TestClient(app)
    fcsrf = login(other, FINANCE)
    headers = {"X-CSRF-Token": fcsrf}
    assert other.get("/api/conversations").json() == []
    assert other.get(f"/api/conversations/{cid}").status_code == 404
    assert (
        other.patch(f"/api/conversations/{cid}", json={"title": "x"}, headers=headers).status_code
        == 404
    )
    assert other.delete(f"/api/conversations/{cid}", headers=headers).status_code == 404
    assert _ask(other, fcsrf, "Continue their chat?", cid).status_code == 404


def test_rename_and_delete(client, api, db):
    plans, _ = api
    plans.append({"kind": "clarify", "clarification": "Which?"})
    csrf = login(client, OPS)
    cid = _ask(client, csrf, "Something vague?").json()["conversation_id"]
    headers = {"X-CSRF-Token": csrf}
    resp = client.patch(f"/api/conversations/{cid}", json={"title": "Lamps"}, headers=headers)
    assert resp.json()["title"] == "Lamps"

    assert client.delete(f"/api/conversations/{cid}").status_code == 403  # CSRF required
    assert client.delete(f"/api/conversations/{cid}", headers=headers).status_code == 204
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    # The turns and their model-call logs are gone too (database cascade).
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(AnswerRun)) == 0
    assert db.scalar(select(func.count()).select_from(LlmCall)) == 0


def test_turn_using_a_revoked_source_is_redacted_when_reopened(client, api, db):
    plans, answers = api
    plans.append(IRELAND_MARCH)
    answers.append({"answer": "Ireland: -€509.00 [D1]."})
    csrf = login(client, OPS)
    cid = _ask(client, csrf, "Ireland net sales March 2026?").json()["conversation_id"]
    returns = db.scalar(select(Source).where(Source.slug == "returns"))
    db.query(SourceGrant).filter(SourceGrant.source_id == returns.id).delete()
    db.commit()
    turn0 = client.get(f"/api/conversations/{cid}").json()["turns"][0]
    assert (
        turn0["status"] == "redacted" and turn0["evidence"] == [] and "509" not in json.dumps(turn0)
    )
    assert turn0["question"] == "Ireland net sales March 2026?"  # the user's own words stay
