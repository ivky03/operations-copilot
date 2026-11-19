"""Workflow tests for the answer pipeline, using a scripted fake model.

These prove the pipeline's LOGIC (planning → grounding → SQL → writing → verification →
fallback, retries, provider failover, persistence) deterministically and for free.
They say nothing about MODEL QUALITY; that is measured separately against the real model
(evals, M3c).
"""

import json
import time

import httpx
import pytest
from sqlalchemy import func, select

from app.answer.evidence import EvidenceItem
from app.answer.pipeline import answer_question
from app.answer.verify import verify
from app.api.ask import get_planner_llm, get_writer_llm
from app.cli import publish_reference, seed_reference
from app.llm.client import (
    CallRecord,
    Completion,
    LLMClient,
    LLMUnavailableError,
    OpenAICompatibleProvider,
)
from app.main import app
from app.models import AnswerRun, LlmCall, Source, SourceGrant, User
from tests.conftest import login

OPS = "ops.employee@brindlewood.example"


class FakeProvider:
    """Returns scripted replies in order and records every prompt it was sent."""

    def __init__(self, replies: list, name: str = "fake"):
        self.name, self.model = name, "fake-model"
        self.replies = list(replies)
        self.prompts: list[list[dict]] = []

    def complete(self, messages, timeout):
        self.prompts.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return Completion(text, prompt_tokens=100, completion_tokens=20)


def fake(*replies) -> tuple[LLMClient, FakeProvider]:
    provider = FakeProvider(list(replies))
    return LLMClient([provider], retry_delay=0), provider


IRELAND_MARCH = {
    "kind": "data",
    "queries": [
        {
            "metric": "net_sales",
            "filters": [{"dimension": "country", "values": ["Ireland"]}],
            "time_range": {"start": "2026-03-01", "end": "2026-04-01"},
        }
    ],
}


@pytest.fixture
def olivia(db):
    seed_reference(db)
    publish_reference(db)
    return db.scalar(select(User).where(User.email == OPS))


def ask(db, user, question, planner_replies, writer_replies):
    planner, p = fake(*planner_replies)
    writer, w = fake(*writer_replies)
    return answer_question(db, user, question, planner, writer), p, w


# ----------------------------------------------------------------------------- happy path


def test_answer_uses_computed_numbers_and_is_saved_with_its_trace(db, olivia):
    result, planner, _ = ask(
        db,
        olivia,
        "What were Ireland's net sales in March 2026?",
        [IRELAND_MARCH],
        [{"answer": "Ireland's net sales in March 2026 were -€509.00 [D1]."}],
    )
    assert result.status == "answered"
    assert result.evidence[0].rows == [{"net_sales": -509.0}]
    assert {(s["source"], s["version"]) for s in result.sources} == {
        ("transactions", 1),
        ("returns", 1),
        ("stores", 1),
    }
    run = db.get(AnswerRun, result.run_id)
    assert run.status == "answered" and run.verification["ok"] is True
    assert (
        db.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.answer_run_id == run.id))
        == 2
    )


def test_prompt_contains_schema_but_never_data_rows(db, olivia):
    _, planner, _ = ask(
        db, olivia, "Net sales in March?", [IRELAND_MARCH], [{"answer": "-€509.00 [D1]"}]
    )
    prompt = json.dumps(planner.prompts[0])
    assert "net_sales" in prompt and "store_name" in prompt  # schema is described
    assert "INV-100" not in prompt and "C1001" not in prompt  # no invoice or customer rows


def test_restricted_datasets_never_reach_the_prompt(db, olivia):
    returns = db.scalar(select(Source).where(Source.slug == "returns"))
    db.query(SourceGrant).filter(SourceGrant.source_id == returns.id).delete()
    db.commit()
    _, planner, _ = ask(db, olivia, "Refunds?", [{"kind": "unanswerable", "reason": "no data"}], [])
    prompt = json.dumps(planner.prompts[0])
    assert "refund_value" not in prompt and "Customer returns" not in prompt


# ----------------------------------------------------------------------------- verification


def test_invented_number_triggers_a_retry_with_feedback(db, olivia):
    result, _, writer = ask(
        db,
        olivia,
        "Ireland net sales March 2026?",
        [IRELAND_MARCH],
        [
            {"answer": "Ireland made €1,234.00 [D1]."},  # invented
            {"answer": "Ireland's net sales were -€509.00 [D1]."},
        ],
    )
    assert result.status == "answered"
    assert "1,234" in json.dumps(writer.prompts[1])  # the feedback named the bad number


def test_two_failed_verifications_fall_back_to_a_table(db, olivia):
    result, _, _ = ask(
        db,
        olivia,
        "Ireland net sales March 2026?",
        [IRELAND_MARCH],
        [{"answer": "About €9,999 [D1]."}, {"answer": "Roughly €8,888 [D1]."}],
    )
    assert result.status == "answered_fallback"
    assert "-€509.00" in result.answer and "9,999" not in result.answer


# ----------------------------------------------------------------------------- grounding


def test_planner_narrowing_an_ambiguous_phrase_is_caught(db, olivia):
    narrowed = {
        "kind": "data",
        "queries": [
            {
                "metric": "units_sold",
                "filters": [{"dimension": "product_name", "values": ["Blue Desk Lamp"]}],
            }
        ],
    }
    result, _, _ = ask(db, olivia, "How many desk lamps did we sell?", [narrowed], [])
    assert result.status == "clarification"
    assert set(result.options) == {"Blue Desk Lamp", "White Desk Lamp", "Desk Lamp Shade"}


def test_alias_is_applied_and_shown_as_a_note(db, olivia):
    plan = {
        "kind": "data",
        "queries": [
            {
                "metric": "order_count",
                "filters": [{"dimension": "country", "values": ["UK"]}],
            }
        ],
    }
    result, _, _ = ask(db, olivia, "How many UK orders?", [plan], [{"answer": "x [D1]"}])
    assert "Interpreted “UK” as “United Kingdom”." in result.notes


def test_unknown_value_is_explained_not_guessed(db, olivia):
    plan = {
        "kind": "data",
        "queries": [
            {
                "metric": "gross_sales",
                "filters": [{"dimension": "country", "values": ["Narnia"]}],
            }
        ],
    }
    result, _, _ = ask(db, olivia, "Gross sales in Narnia?", [plan], [])
    assert result.status == "unanswerable" and "Narnia" in result.answer


# ----------------------------------------------------------------------------- planning


def test_invalid_metric_in_plan_gets_one_retry(db, olivia):
    bad = {"kind": "data", "queries": [{"metric": "revenue"}]}
    good = {"kind": "data", "queries": [{"metric": "gross_sales"}]}
    result, planner, _ = ask(
        db, olivia, "Total revenue?", [bad, good], [{"answer": "Gross sales were shown [D1]."}]
    )
    assert len(planner.prompts) == 2 and "Unknown metric" in json.dumps(planner.prompts[1])
    assert result.evidence[0].metric == "gross_sales"


def test_malformed_json_is_repaired_once(db, olivia):
    result, planner, _ = ask(
        db,
        olivia,
        "Unclear?",
        ["not json at all", {"kind": "clarify", "clarification": "Which metric?"}],
        [],
    )
    assert result.status == "clarification" and len(planner.prompts) == 2


@pytest.mark.parametrize(
    ("kind", "status"), [("unanswerable", "unanswerable"), ("documents", "documents")]
)
def test_non_data_questions_are_not_answered_with_numbers(db, olivia, kind, status):
    result, _, _ = ask(db, olivia, "q?", [{"kind": kind, "reason": "because"}], [])
    assert result.status == status and result.evidence == []


# ----------------------------------------------------------------------------- providers


def _transport(responses: list[httpx.Response]):
    def handler(request):
        return responses.pop(0)

    return httpx.MockTransport(handler)


def _ok(content: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def test_rate_limit_is_waited_out_using_the_providers_hint():
    provider = OpenAICompatibleProvider(
        "groq",
        "https://example.test",
        "key",
        "m",
        transport=_transport(
            [httpx.Response(429, headers={"retry-after": "0"}), _ok({"kind": "clarify"})]
        ),
    )
    calls: list[CallRecord] = []
    from app.answer.planner import AnswerPlan

    plan = LLMClient([provider], retry_delay=0).complete_json("plan", [], AnswerPlan, calls)
    assert plan.kind == "clarify"
    assert [c.status for c in calls] == ["error", "ok"] and calls[0].error == "HTTP 429"


def test_long_rate_limit_wait_switches_provider_instead_of_waiting():
    from app.answer.planner import AnswerPlan

    busy = OpenAICompatibleProvider(
        "groq",
        "https://x.test",
        "k",
        "m",
        transport=_transport([httpx.Response(429, headers={"retry-after": "20"})]),
    )
    up = OpenAICompatibleProvider(
        "gemini", "https://y.test", "k", "m", transport=_transport([_ok({"kind": "clarify"})])
    )
    calls: list[CallRecord] = []
    start = time.monotonic()
    LLMClient([busy, up], retry_delay=0).complete_json("plan", [], AnswerPlan, calls)
    assert time.monotonic() - start < 2  # did not sit out the 20 s
    assert [(c.provider, c.status) for c in calls] == [("groq", "error"), ("gemini", "ok")]


def test_failover_to_the_second_provider():
    from app.answer.planner import AnswerPlan

    down = OpenAICompatibleProvider(
        "groq",
        "https://x.test",
        "k",
        "m",
        transport=_transport([httpx.Response(503), httpx.Response(503)]),
    )
    up = OpenAICompatibleProvider(
        "gemini", "https://y.test", "k", "m", transport=_transport([_ok({"kind": "clarify"})])
    )
    calls: list[CallRecord] = []
    LLMClient([down, up], retry_delay=0).complete_json("plan", [], AnswerPlan, calls)
    assert [(c.provider, c.status) for c in calls] == [
        ("groq", "error"),
        ("groq", "error"),
        ("gemini", "ok"),
    ]


def test_all_providers_down_is_a_clear_error_and_still_saved(db, olivia):
    broken, _ = fake(httpx.ConnectError("down"), httpx.ConnectError("down"))
    writer, _ = fake()
    result = answer_question(db, olivia, "Anything?", broken, writer)
    assert result.status == "error" and "unavailable" in result.answer
    assert db.get(AnswerRun, result.run_id).status == "error"


def test_no_provider_configured():
    from app.answer.planner import AnswerPlan

    with pytest.raises(LLMUnavailableError):
        LLMClient([]).complete_json("plan", [], AnswerPlan, [])


# ----------------------------------------------------------------------------- verifier unit tests


def _item(rows, unit="EUR", metric="net_sales", comparisons=()):
    return EvidenceItem(
        "D1",
        metric,
        "t",
        unit,
        "d",
        list(rows[0]) if rows else [metric],
        rows,
        "sql",
        [],
        list(comparisons),
    )


@pytest.mark.parametrize(
    ("answer", "ok"),
    [
        ("Net sales were €7,070.00 [D1].", True),
        ("Net sales were €7,070 [D1].", True),  # rounded for display
        ("Net sales were €7,071.00 [D1].", False),  # not in evidence
        ("In March 2026 net sales were €7,070.00 [D1].", True),  # year allowed
        ("Net sales were €7,070.00.", False),  # no citation
        ("Net sales were €7,070.00 [D2].", False),  # unknown citation
    ],
)
def test_verifier(answer, ok):
    assert verify(answer, [_item([{"net_sales": 7070.0}])], "net sales?").ok is ok


def test_verifier_accepts_percentages_for_ratios_and_code_computed_changes():
    ratio = _item([{"cancellation_rate": 0.0526}], unit="ratio", metric="cancellation_rate")
    assert verify("The rate was 5.26% [D1].", [ratio], "q").ok
    comp = _item(
        [{"month": "2026-02", "net_sales": 7070.0}, {"month": "2026-03", "net_sales": 8003.5}],
        comparisons=[
            {"from_value": 7070.0, "to_value": 8003.5, "change": 933.5, "change_pct": 13.2}
        ],
    )
    assert verify("Up €933.50 (+13.2%) [D1].", [comp], "q").ok
    assert not verify("Up 15% [D1].", [comp], "q").ok


# ----------------------------------------------------------------------------- API


def test_ask_endpoint(client, db, olivia):
    from app.security import hash_password

    olivia.password_hash = hash_password("a-known-password-123")  # seeded passwords are random
    db.commit()
    planner, _ = fake(IRELAND_MARCH)
    writer, _ = fake({"answer": "Ireland: -€509.00 [D1]."})
    app.dependency_overrides[get_planner_llm] = lambda: planner
    app.dependency_overrides[get_writer_llm] = lambda: writer
    try:
        csrf = login(client, OPS, "a-known-password-123")
        resp = client.post(
            "/api/ask",
            json={"question": "Ireland net sales March 2026?"},
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "answered" and body["evidence"][0]["rows"] == [{"net_sales": -509.0}]
