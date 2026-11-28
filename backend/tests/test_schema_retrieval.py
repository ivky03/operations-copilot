"""Schema retrieval (M8b): choosing which tables the planner is shown.

What must hold, whatever the model:
- every dataset version gets one card when it's prepared;
- only the user's own catalog is searched, so a table they can't read is never chosen;
- chosen tables bring the tables they need (links, formulas), so their metrics stay usable;
- a table without a card is shown anyway, never silently hidden;
- in "retrieved" mode the planner's prompt describes only the chosen tables, the choice is saved
  with the run, and a follow-up keeps the previous question's tables;
- on Stage C, retrieval finds every table each evaluation question needs (the recall gate).

Leakage through retrieval (restricted, revoked, archived, deleted) is probed in test_leakage.py.
"""

import pytest
from sqlalchemy import delete, func, select

from app.answer import cache
from app.answer.context import build_context
from app.answer.pipeline import answer_question
from app.cli import publish_reference, seed_reference
from app.config import get_settings
from app.models import AnswerRun, Conversation, SchemaEntry, SourceVersion, User
from app.query.catalog import Catalog
from app.query.schema_retrieval import card_text, search_text, select_tables
from tests.test_answer_pipeline import IRELAND_MARCH, fake
from tests.test_stage_c import published  # noqa: F401 - fixture: Stage C seeded and published

OPS, FIN = "ops@example.com", "fin@example.com"


def test_dates_are_removed_before_searching():
    assert search_text("What was our revenue in March 2026?") == "What was our revenue in ?"
    assert search_text("Stock at the end of 31 March 2026, and 2026-04-01 in Q1") == (
        "Stock at the end of , and in"
    )
    assert search_text("Units sold per store") == "Units sold per store"


def test_a_card_says_what_a_table_is_for_and_lists_its_metrics_not_its_columns():
    import yaml

    from app.ingest.manifest import Manifest
    from evals.schema_eval import DATA

    m = Manifest.model_validate(
        yaml.safe_load((DATA / "reference" / "manifests" / "transactions.yaml").read_text())
    )
    text = card_text(m)
    assert text.startswith("Sales transactions. Every sales and cancellation line")
    assert "Net sales: Gross sales minus cancellations minus refunds" in text
    assert "invoice_no" not in text  # columns are left out


# ----------------------------------------------------------------------------- database


@pytest.fixture
def stage_b(db, make_user):
    seed_reference(db, "b")
    publish_reference(db, "b")
    make_user(OPS, groups=("operations",))
    make_user(FIN, groups=("operations", "finance"))
    return db


def user(db, email) -> User:
    db.expire_all()
    return db.scalar(select(User).where(User.email == email))


@pytest.fixture
def retrieved(monkeypatch):
    monkeypatch.setenv("SCHEMA_MODE", "retrieved")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("SCHEMA_MODE")
    get_settings.cache_clear()


@pytest.mark.integration
def test_every_dataset_version_gets_one_card_when_prepared(stage_b):
    db = stage_b
    versions = db.scalar(
        select(func.count()).select_from(SourceVersion).where(SourceVersion.meta.has_key("table"))
    )
    assert versions == 15
    assert db.scalar(select(func.count()).select_from(SchemaEntry)) == 15


@pytest.mark.integration
def test_chosen_tables_bring_the_tables_they_need(stage_b):
    db = stage_b
    catalog = Catalog.for_user(db, user(db, OPS))
    chosen = select_tables(db, catalog, ["What were net sales by country in March 2026?"], k=1)
    assert chosen.found == ["transactions"]
    # net_sales subtracts returns.refund_value; country is on stores; products is linked too.
    assert set(chosen.added) == {"returns", "stores", "products"}


@pytest.mark.integration
def test_only_the_users_own_tables_are_searched(stage_b):
    """Operations can't read budgets or targets. Asking for them by name finds other tables:
    the restricted cards are never compared, so they can't be chosen or change the ranking."""
    db = stage_b
    question = ["What was the budgeted cost and the net sales target for March?"]
    ops = select_tables(db, Catalog.for_user(db, user(db, OPS)), question, k=5)
    fin = select_tables(db, Catalog.for_user(db, user(db, FIN)), question, k=5)
    assert {"budgets", "sales_targets"} <= set(fin.found)
    assert not {"budgets", "sales_targets"} & ops.tables
    assert len(ops.found) == 5


@pytest.mark.integration
def test_a_table_without_a_card_is_shown_rather_than_hidden(stage_b):
    db = stage_b
    catalog = Catalog.for_user(db, user(db, OPS))
    staff = catalog.datasets["staff"]
    db.execute(delete(SchemaEntry).where(SchemaEntry.source_version_id == staff.version_id))
    chosen = select_tables(db, catalog, ["How many units were returned?"], k=2)
    assert chosen.unindexed == ["staff"] and "staff" in chosen.tables
    assert "staff" not in chosen.found


@pytest.mark.integration
def test_the_planner_is_shown_only_the_chosen_tables(stage_b, retrieved):
    db = stage_b
    olga = user(db, OPS)
    planner, p = fake(IRELAND_MARCH)
    writer, _ = fake({"answer": "Ireland's net sales in March 2026 were -€509.00 [D1]."})
    question = "What were Ireland's net sales in March 2026?"
    result = answer_question(db, olga, question, planner, writer)
    assert result.status == "answered"
    prompt = p.prompts[0][1]["content"]
    assert "## Dataset `transactions`" in prompt and "## Dataset `stores`" in prompt
    assert "## Dataset `staff`" not in prompt and "## Dataset `shifts`" not in prompt
    run = db.get(AnswerRun, result.run_id)
    assert run.plan["schema"]["mode"] == "retrieved"
    assert run.plan["schema"]["found"][0] == "transactions"
    full = build_context(db, Catalog.for_user(db, olga)).text
    assert len(prompt) < len(full)


@pytest.mark.integration
def test_a_follow_up_keeps_the_previous_questions_tables(stage_b, retrieved):
    db = stage_b
    olga = user(db, OPS)
    chat = Conversation(user_id=olga.id, title="Shifts")
    db.add(chat)
    db.commit()
    hours = {"kind": "data", "queries": [{"metric": "hours_worked", "dimensions": ["store_name"]}]}
    planner, _ = fake(hours)
    writer, _ = fake({"answer": "See [D1]."})
    answer_question(db, olga, "How many hours did staff work at each store?", planner, writer, chat)
    planner, p = fake(hours)
    writer, _ = fake({"answer": "See [D1]."})
    answer_question(db, olga, "And just in April?", planner, writer, chat)
    assert "## Dataset `shifts`" in p.prompts[0][1]["content"]


@pytest.mark.integration
def test_how_tables_are_chosen_is_part_of_the_cache_key(stage_b, monkeypatch):
    db = stage_b
    readable = cache.readable_versions(db, user(db, OPS))
    planner, _ = fake()
    full = cache.cache_key("Net sales?", readable, planner, planner)
    monkeypatch.setenv("SCHEMA_MODE", "retrieved")
    get_settings.cache_clear()
    try:
        assert cache.cache_key("Net sales?", readable, planner, planner) != full
    finally:
        monkeypatch.delenv("SCHEMA_MODE")
        get_settings.cache_clear()


# ----------------------------------------------------------------------------- Stage C


@pytest.mark.integration
def test_retrieval_finds_every_table_stage_c_questions_need(db, published):  # noqa: F811
    """The recall gate, at the default k. The tuning set (the 47 answer-key questions, used to
    design retrieval) must be complete at every experiment size. The held-out set (written before
    retrieval was measured on it) has one known miss: "Which shop brought in the most money?"
    finds no sales table, because no table says "money". A new miss fails this test."""
    from evals.schema_eval import evaluate, load_cases, summarise

    _, users = published
    k = get_settings().schema_top_k
    assert k == 5
    tuning = summarise(evaluate(db, users, load_cases("tuning"), (k,)))
    assert [(r["size"], r["complete"], r["cases"]) for r in tuning] == [
        (4, 8, 8),
        (15, 16, 16),
        (30, 32, 32),
        (50, 47, 47),
    ]
    heldout = summarise(evaluate(db, users, load_cases("heldout"), (k,)))
    misses = {m.split(" ")[0] for r in heldout for m in r["missed"]}
    assert misses == {"h-02"}, heldout
    at_50 = next(r for r in tuning if r["size"] == 50)
    assert at_50["context_tokens"] < 0.25 * at_50["full_tokens"]  # about 1,900 vs 9,600
