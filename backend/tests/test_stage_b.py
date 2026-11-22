"""Stage B loads through the normal publish path, keeps its permission boundaries (including in
what the planner is shown), and the query engine computes its new metrics correctly.

Expected values are computed here with pandas straight from the CSV files, sharing no code
with the query compiler.
"""

import pandas as pd
import pytest
from sqlalchemy import select

from app.answer.context import build_context
from app.answer.passages import document_titles
from app.cli import STAGE_B, publish_reference, seed_reference
from app.models import Group, Role, User
from app.query.catalog import Catalog
from app.query.compiler import compile_plan
from app.query.executor import execute
from app.query.plan import QueryPlan
from app.security import hash_password

pytestmark = pytest.mark.integration

TABLES = STAGE_B / "tables"


@pytest.fixture
def stage_b(db):
    seed_reference(db, "b")
    results = publish_reference(db, "b")
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


def run(db, user, **plan):
    return execute(compile_plan(QueryPlan(**plan), Catalog.for_user(db, user))).rows


def test_every_stage_b_source_publishes(stage_b):
    results, _ = stage_b
    assert [r.source for r in results if r.status != "published"] == []
    # 15 datasets + 21 documents (22 document files: the returns policy has two versions)
    assert len({r.source for r in results}) == 15 + 21


def test_each_group_sees_its_own_tables_and_nothing_of_the_others(db, stage_b):
    _, users = stage_b
    ops, fin = Catalog.for_user(db, users["operations"]), Catalog.for_user(db, users["finance"])
    assert {"staff", "shifts"} <= set(ops.datasets) and not {"sales_targets", "budgets"} & set(
        ops.datasets
    )
    assert {"sales_targets", "budgets"} <= set(fin.datasets) and not {"staff", "shifts"} & set(
        fin.datasets
    )
    # What the planner is shown must not even name the other group's tables or metrics.
    fin_prompt = build_context(db, fin).text.lower()
    for leaked in ("shifts", "headcount", "absence", "staff."):
        assert leaked not in fin_prompt, leaked
    ops_prompt = build_context(db, ops).text.lower()
    for leaked in ("sales_targets", "net_sales_target", "budget"):
        assert leaked not in ops_prompt, leaked
    ops_docs = document_titles(db, users["operations"])
    assert not any("Budget Guidelines" in str(d) for d in ops_docs)
    assert any("Budget Guidelines" in str(d) for d in document_titles(db, users["finance"]))


def test_new_metrics_match_an_independent_calculation(db, stage_b):
    _, users = stage_b
    ops, fin = users["operations"], users["finance"]
    staff = pd.read_csv(TABLES / "staff.csv")
    stores = pd.read_csv(STAGE_B.parent / "reference" / "tables" / "stores.csv")

    # Headcount in Berlin: still employed at the end of the data (a March leaver excluded,
    # a March joiner included), joined through stores.
    berlin = staff.merge(stores, on="store_id")
    expected = int(((berlin.store_name == "Berlin Store") & berlin.is_active).sum())
    rows = run(
        db,
        ops,
        metric="headcount",
        filters=[{"dimension": "store_name", "values": ["Berlin Store"]}],
    )
    assert rows[0]["headcount"] == expected == 5

    # On-time delivery rate by carrier: in-transit parcels excluded from the denominator.
    shipments = pd.read_csv(TABLES / "shipments.csv")
    delivered = shipments[shipments.status == "delivered"]
    expected = (delivered.groupby("carrier").is_on_time.mean()).round(4).to_dict()
    rows = run(db, ops, metric="on_time_delivery_rate", dimensions=["carrier"])
    assert {r["carrier"]: round(float(r["on_time_delivery_rate"]), 4) for r in rows} == expected

    # Stock is point-in-time: one day's units on hand, by country.
    inv = pd.read_csv(TABLES / "inventory_snapshots.csv").merge(stores, on="store_id")
    day = inv[inv.snapshot_date == "2026-03-31"].groupby("country").units_on_hand.sum().to_dict()
    rows = run(
        db,
        ops,
        metric="units_on_hand",
        dimensions=["country"],
        time_range={"start": "2026-03-31", "end": "2026-04-01"},
    )
    assert {r["country"]: int(r["units_on_hand"]) for r in rows} == day

    # Average CSAT ignores customers who didn't answer.
    tickets = pd.read_csv(TABLES / "support_tickets.csv")
    rows = run(db, fin, metric="average_csat")
    assert round(float(rows[0]["average_csat"]), 4) == round(tickets.csat_score.mean(), 4)

    # April budget actuals are empty, not zero: actual cost for April is null/absent.
    rows = run(
        db, fin, metric="actual_cost", time_range={"start": "2026-04-01", "end": "2026-05-01"}
    )
    assert rows == [] or rows[0]["actual_cost"] in (None, 0)


def test_a_restricted_metric_can_not_be_queried_by_the_other_group(db, stage_b):
    from app.query.catalog import PlanError

    _, users = stage_b
    with pytest.raises(PlanError):
        run(db, users["finance"], metric="headcount")
    with pytest.raises(PlanError):
        run(db, users["operations"], metric="net_sales_target")
