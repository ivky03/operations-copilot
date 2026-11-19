"""Live check against the real language model. Skipped unless RUN_LLM_TESTS=1 and a Groq key is
configured, so CI and normal test runs never spend free-tier quota. Model QUALITY is measured
by the evaluation runner (M3c); this only proves the real integration works end to end."""

import os

import pytest
from sqlalchemy import select

from app.answer.pipeline import answer_question
from app.api.ask import get_planner_llm, get_writer_llm
from app.cli import publish_reference, seed_reference
from app.config import get_settings
from app.models import User

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(
        os.environ.get("RUN_LLM_TESTS") != "1" or not get_settings().groq_api_key,
        reason="set RUN_LLM_TESTS=1 and GROQ_API_KEY to call the real model",
    ),
]


def test_headline_question_end_to_end(db):
    seed_reference(db)
    publish_reference(db)
    olivia = db.scalar(select(User).where(User.email == "ops.employee@brindlewood.example"))
    result = answer_question(
        db,
        olivia,
        "Compare February and March net sales by country",
        get_planner_llm(),
        get_writer_llm(),
    )
    assert result.status == "answered", result.answer
    rows = {(r["month"], r["country"]): r["net_sales"] for r in result.evidence[0].rows}
    assert rows[("2026-03", "Ireland")] == -509.0
    assert ("2026-02", "France") not in rows
