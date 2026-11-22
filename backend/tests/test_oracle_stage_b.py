"""The Stage B answer key follows the same rules a database would (on the real Stage B CSVs)."""

import pandas as pd
import pytest

from evals.oracle import STAGE_B_DATA, Oracle, Query


@pytest.fixture(scope="module")
def oracle():
    return Oracle.for_stage("stage_b")


def test_an_empty_group_is_kept_like_sql_group_by(oracle):
    """Central customer service has no store; its shifts must not silently disappear."""
    rows = oracle.run(Query("shift_count", ["store_name"]))
    total = len(pd.read_csv(STAGE_B_DATA / "shifts.csv"))
    assert sum(r["shift_count"] for r in rows) == total
    assert any(r["store_name"] is None for r in rows)


def test_a_sum_over_only_empty_values_is_empty_not_zero(oracle):
    rows = oracle.run(Query("actual_cost", ["month"]))
    assert {r["month"]: r["actual_cost"] is None for r in rows}["2026-04"]
    assert all(r["actual_cost"] > 0 for r in rows if r["month"] != "2026-04")


def test_the_metric_tables_own_column_wins_a_name_clash(oracle):
    """A support ticket's channel is email/chat/phone, not the store's Retail/Online."""
    channels = {r["channel"] for r in oracle.run(Query("ticket_count", ["channel"]))}
    assert channels == {"email", "chat", "phone"}


def test_a_name_found_on_two_linked_tables_is_rejected(oracle):
    with pytest.raises(ValueError, match="products.*suppliers"):
        oracle.run(Query("units_ordered", ["category"]))


def test_stock_on_one_day_is_not_a_sum_over_days(oracle):
    one_day = oracle.run(Query("units_on_hand", filters={"day": ["2026-03-31"]}))[0]
    whole_month = oracle.run(Query("units_on_hand", filters={"month": ["2026-03"]}))[0]
    assert whole_month["units_on_hand"] > 20 * one_day["units_on_hand"]


def test_stage_a_oracle_has_no_stage_b_data():
    with pytest.raises(ValueError, match="no Stage B data"):
        Oracle().run(Query("headcount"))
