"""Stage C: the committed files are exactly what the generator writes, the tables agree with each
other (and with the documents) wherever they describe the same thing, everything loads through
the normal publish path, and the query engine agrees with the independent answer key on every
Stage C evaluation question."""

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime

import pytest
from sqlalchemy import select

from app.cli import STAGE_C, publish_reference, seed_reference
from app.models import Group, Role, User
from app.security import hash_password
from datagen.stage_c import SIZE_30, generate_stage_c
from datagen.stage_c_docs import generate_documents
from tests.test_stage_b import plan_from_oracle_spec, run

DATA = STAGE_C.parent


def rows(stage: str, table: str) -> list[dict]:
    with (DATA / stage / "tables" / f"{table}.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def stage_c():
    return generate_stage_c()


# ----------------------------------------------------------------------------- files


def test_committed_files_match_the_generator(tmp_path):
    from datagen.__main__ import write_stage_c

    write_stage_c(tmp_path)
    for path in sorted(tmp_path.rglob("*")):
        if path.is_file():
            committed = STAGE_C / path.relative_to(tmp_path)
            assert committed.read_bytes() == path.read_bytes(), (
                f"{committed.relative_to(DATA)} is stale: run `uv run python -m datagen --stage c`"
            )
    generated = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}
    committed = {
        p.relative_to(STAGE_C) for p in STAGE_C.rglob("*") if p.is_file() and p.name != "README.md"
    }
    assert generated == committed, "a file was added or removed by hand"


def test_fifty_tables_and_the_thirty_table_step(stage_c):
    stage_ab = {
        p.stem for d in ("reference", "stage_b") for p in (DATA / d / "tables").glob("*.csv")
    }
    assert len(stage_ab) == 15 and len(stage_c) == 35
    assert set(SIZE_30) <= set(stage_c) and len(SIZE_30) == 15
    names = Counter(
        m["name"]
        for t in stage_c.values()
        for m in t.manifest.get("metrics", []) + t.manifest.get("derived_metrics", [])
    )
    assert [n for n, c in names.items() if c > 1] == []


# ----------------------------------------------------------------------------- consistency


def test_web_analytics_and_campaign_credit_come_from_real_orders(stage_c):
    """The browser saw every real online order (before any cancellation): the same count per
    shop and day. Campaign credit is real orders credited to campaigns, so it is never more
    than the real orders times the number of campaigns running."""
    orders = defaultdict(set)
    for t in rows("reference", "transactions"):
        if t["is_cancellation"] == "false" and t["store_id"] in {"S02", "S05", "S08"}:
            orders[(t["store_id"], t["invoice_date"][:10])].add(t["invoice_no"])
    tracked = Counter()
    sessions = stage_c["web_sessions_daily"]
    for r in sessions.rows:
        tracked[(r[1], r[0].isoformat())] += r[7]
    assert {k: v for k, v in tracked.items() if v} == {k: len(v) for k, v in orders.items()}


def test_tables_and_documents_state_the_same_facts(stage_c):
    docs = {d.filename: d.text for d in generate_documents()}
    # Carriers: the names shipments use.
    shipped = {s["carrier"] for s in rows("stage_b", "shipments")}
    carriers = {r[0] for r in stage_c["carriers"].rows}
    assert shipped <= carriers
    # Supplier agreements: the lead time and payment terms in the suppliers table.
    for s in rows("stage_b", "suppliers"):
        text = next(
            t for f, t in docs.items() if s["supplier_name"] in t and f.startswith("supplier-")
        )
        assert f"Standard lead time is {s['lead_time_days']} days" in text
        assert f"payable {s['payment_terms_days']} days" in text
    # Campaign briefs: the budget in the campaigns table.
    for c in stage_c["marketing_campaigns"].rows:
        text = next(t for t in docs.values() if f"({c[0]})" in t)
        assert f"EUR {int(c[5]):,}" in text
    # Opening hours: the handbook's 09:00-20:00 / 11:00-17:00, with the documented exceptions.
    for store, weekday, opens, closes, late, closed in stage_c["opening_hours"].rows:
        if closed:
            assert weekday == "Sunday" and store == "S04"
        elif weekday == "Sunday":
            assert (opens, closes) == ("11:00", "17:00")
        else:
            assert (opens, closes) == ("09:00", "21:00" if late else "20:00")
    # Returns window: 30 days everywhere, as the returns policy says.
    assert {r[4] for r in stage_c["product_categories"].rows} == {30}


def test_ledger_revenue_and_loyalty_points_follow_sales(stage_c):
    sales = defaultdict(float)
    for t in rows("reference", "transactions"):
        month = t["invoice_date"][:7]
        sales[(t["store_id"], month)] += int(t["quantity"]) * float(t["unit_price"])  # CINV < 0
    for r in stage_c["gl_entries"].rows:
        if r[2] == "4000":
            expected = sales[(r[6], r[1].isoformat()[:7])]
            assert abs(r[7] - expected) <= 0.031 * expected + 0.01, r
    value = {}
    for t in rows("reference", "transactions"):
        if t["is_cancellation"] == "false":
            value.setdefault(t["invoice_no"], 0)
            value[t["invoice_no"]] += int(int(t["quantity"]) * float(t["unit_price"]))
    earned = defaultdict(int)
    for r in stage_c["loyalty_points"].rows:
        if r[6] == "earn":
            earned[r[4]] += r[5]
    assert all(earned[inv] == value[inv] for inv in earned)


def test_head_office_staff_have_no_store_and_that_is_allowed(stage_c):
    no_store = {e["employee_id"] for e in rows("stage_b", "staff") if not e["store_id"]}
    assert no_store
    payroll = stage_c["payroll"]
    store_col = payroll.header.index("store_id")
    assert any(r[store_col] is None for r in payroll.rows)
    column = next(c for c in payroll.manifest["columns"] if c["name"] == "store_id")
    assert column["nullable"] is True


# ----------------------------------------------------------------------------- database


@pytest.fixture
def published(db):
    seed_reference(db, "c")
    results = publish_reference(db, "c")
    users = {}
    for group in ("operations", "finance"):
        user = User(
            email=f"{group}@example.com",
            display_name=group,
            password_hash=hash_password("not used here at all"),
            role=Role.EMPLOYEE,
            groups=[db.scalar(select(Group).where(Group.slug == group))],
        )
        db.add(user)
        users[group] = user
    db.commit()
    return results, users


@pytest.mark.integration
def test_stage_c_publishes_and_the_engine_agrees_with_the_answer_key(db, published):
    """Given the right plan, the query engine reproduces every expected Stage C answer. So in the
    real-model experiment, a wrong answer is the planner's choice, not an engine bug."""
    from evals.answer_eval import rows_match
    from evals.golden import GOLDEN_DIR, iter_steps, load_golden
    from evals.run_answer_eval import check_stage

    results, users = published
    assert [r.source for r in results if r.status != "published"] == []
    assert len({r.source for r in results}) == 50 + 114
    check_stage(db, "stage_c")

    expected = json.loads((GOLDEN_DIR / "stage_c.expected.json").read_text())
    checked = 0
    for case in load_golden("stage_c")["cases"]:
        for step, _question, _exp in iter_steps(case):
            for item in expected.get(step, []):
                spec = item["query"]
                got = run(db, users[case["persona"]], **plan_from_oracle_spec(spec))
                assert rows_match(item["rows"], got, spec["metric"], False), (
                    step,
                    item["rows"],
                    got,
                )
                checked += 1
    assert checked == 47


@pytest.mark.integration
def test_each_group_reads_only_its_stage_c_tables(db, published):
    from app.query.catalog import Catalog

    _, users = published
    ops = set(Catalog.for_user(db, users["operations"]).datasets)
    fin = set(Catalog.for_user(db, users["finance"]).datasets)
    assert {"payroll", "gl_entries", "product_costs", "expenses", "supplier_invoices"} <= fin - ops
    assert {"training_records", "vacancies", "staff_survey", "shifts"} <= ops - fin
    assert len(ops) == 43 and len(fin) == 45


def test_documents_have_unique_files_and_valid_front_matter():
    import yaml

    seen = set()
    for d in generate_documents():
        meta = yaml.safe_load(d.text.split("---")[1])
        assert meta["doc_id"] == d.doc_id and meta["version"] == d.version
        assert datetime.fromisoformat(str(meta["effective_date"]))
        assert d.filename not in seen
        seen.add(d.filename)
