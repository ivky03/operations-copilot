"""The reference dataset is deterministic, internally consistent, and contains its edge cases."""

from datetime import datetime

import pytest

from datagen.__main__ import DEFAULT_OUT, write_all
from datagen.catalog import PRODUCTS, PROMO_CATEGORY, PROMO_CODE, STORES
from datagen.generate import DATA_END, generate

STORE = {s.store_id: s for s in STORES}
PRODUCT = {p.product_id: p for p in PRODUCTS}


@pytest.fixture(scope="module")
def data():
    return generate()


def test_generation_is_deterministic():
    assert generate() == generate()


def test_committed_csvs_match_the_generator(tmp_path):
    """Drift guard: if someone edits the generator, the committed data must be regenerated."""
    write_all(tmp_path)
    for name in ("stores", "products", "transactions", "returns"):
        assert (tmp_path / f"{name}.csv").read_bytes() == (
            DEFAULT_OUT / f"{name}.csv"
        ).read_bytes(), f"{name}.csv is stale: run `uv run python -m datagen`"


def test_size_is_in_the_planned_range(data):
    lines, returns = data
    assert 300 <= len(lines) <= 500
    assert 15 <= len(returns) <= 30


def test_referential_integrity_and_trading_dates(data):
    lines, _ = data
    for x in lines:
        store, product = STORE[x.store_id], PRODUCT[x.product_id]
        assert store.opened_date <= x.invoice_date.date(), f"{x.line_id}: sold before store opened"
        if not x.is_cancellation and product.discontinued_date:
            assert x.invoice_date.date() <= product.discontinued_date, (
                f"{x.line_id}: sold after discontinued"
            )
        assert x.invoice_date <= DATA_END


def test_cancellations_mirror_their_original_order(data):
    lines, _ = data
    sales = {(x.invoice_no, x.line_no): x for x in lines if not x.is_cancellation}
    cancels = [x for x in lines if x.is_cancellation]
    assert cancels, "expected some cancellations"
    for c in cancels:
        assert c.invoice_no.startswith("C") and c.quantity < 0
        original = sales[(c.invoice_no[1:], c.line_no)]
        assert c.quantity == -original.quantity
        assert c.unit_price == original.unit_price
        assert c.invoice_date > original.invoice_date
    # Whole-order cancellations only: every line of a cancelled order is cancelled.
    cancelled_orders = {c.invoice_no[1:] for c in cancels}
    for inv, line_no in sales:
        if inv in cancelled_orders:
            assert any(c.invoice_no == f"C{inv}" and c.line_no == line_no for c in cancels)


def test_returns_refer_to_kept_sales(data):
    lines, returns = data
    sales = {(x.invoice_no, x.line_no): x for x in lines if not x.is_cancellation}
    cancelled_orders = {x.invoice_no[1:] for x in lines if x.is_cancellation}
    for r in returns:
        sale = sales[(r.invoice_no, r.line_no)]
        assert r.invoice_no not in cancelled_orders, f"{r.return_id}: returned a cancelled order"
        assert 1 <= r.quantity <= sale.quantity
        assert sale.invoice_date < r.return_date <= DATA_END
        assert r.refund_amount == round(r.quantity * sale.unit_price, 2)
        assert (r.store_id, r.product_id) == (sale.store_id, sale.product_id)


def test_deliberate_edge_cases_are_present(data):
    lines, returns = data
    sales = [x for x in lines if not x.is_cancellation]
    cancels = [x for x in lines if x.is_cancellation]

    assert any(x.customer_id is None for x in sales), "guest checkouts"
    assert any(r.reason is None for r in returns), "returns without a reason"
    assert any(x.invoice_date == datetime(2026, 3, 31, 23, 47) for x in sales), "month-end order"
    sale_month = {x.invoice_no: x.invoice_date.month for x in sales}
    assert any(c.invoice_date.month != sale_month[c.invoice_no[1:]] for c in cancels), (
        "a cancellation in a later month than its order"
    )
    first_fr = min(x.invoice_date for x in sales if x.store_id == "S08")
    assert first_fr.date() == STORE["S08"].opened_date or first_fr.date() > STORE["S08"].opened_date
    assert first_fr.month == 3, "FR Online starts trading in March"
    promo = [x for x in lines if x.promo_code == PROMO_CODE]
    assert promo and all(x.invoice_date.month == 4 for x in promo)
    assert all(PRODUCT[x.product_id].category == PROMO_CATEGORY for x in promo)


# ----------------------------------------------------------------------------- Stage B


@pytest.fixture(scope="module")
def stage_b():
    from datagen.stage_b import generate_stage_b

    return {
        name: [dict(zip(t.header, row, strict=True)) for row in t.rows]
        for name, t in generate_stage_b().items()
    }


def test_stage_b_committed_csvs_match_the_generator(tmp_path):
    from datagen.__main__ import STAGE_B_OUT, write_stage_b

    names = write_stage_b(tmp_path)
    assert len(names) == 11
    for name in names:
        assert (tmp_path / f"{name}.csv").read_bytes() == (
            STAGE_B_OUT / f"{name}.csv"
        ).read_bytes(), f"{name}.csv is stale: run `uv run python -m datagen --stage b`"


def test_stage_b_leaves_stage_a_untouched(tmp_path):
    """Stage A's answers stay valid only if its files are byte-for-byte the same."""
    write_all(tmp_path)
    for name in ("stores", "products", "transactions", "returns"):
        assert (tmp_path / f"{name}.csv").read_bytes() == (DEFAULT_OUT / f"{name}.csv").read_bytes()


def test_stage_b_inventory_follows_sales_and_purchase_orders(stage_b, data):
    snapshots = stage_b["inventory_snapshots"]
    assert len(snapshots) >= 20000, "the largest table is in the planned ~20k range"
    assert all(s["units_on_hand"] >= 0 for s in snapshots)
    assert all(s["is_stockout"] == (s["units_on_hand"] == 0) for s in snapshots)
    fr = [s for s in snapshots if s["store_id"] == "S08"]
    assert min(s["snapshot_date"] for s in fr) == STORE["S08"].opened_date

    from datagen.stage_b import SUPPLIERS

    preferred = {s[3]: s[0] for s in SUPPLIERS if s[6]}
    for po in stage_b["purchase_orders"]:
        assert po["supplier_id"] == preferred[PRODUCT[po["product_id"]].category]
        assert (po["received_date"] is None) == (po["status"] in ("open", "cancelled"))
        if po["received_date"]:
            assert po["days_late"] == (po["received_date"] - po["expected_date"]).days
        assert po["quantity_received"] <= po["quantity_ordered"]
    statuses = {po["status"] for po in stage_b["purchase_orders"]}
    assert statuses == {"received", "partially_received", "open", "cancelled"}


def test_stage_b_shipments_tickets_and_summary_agree_with_sales(stage_b, data):
    lines, returns = data
    cancelled = {x.invoice_no[1:] for x in lines if x.is_cancellation}
    for s in stage_b["shipments"]:
        assert STORE[s["store_id"]].channel == "Online"
        assert s["invoice_no"] not in cancelled, "cancelled orders are never shipped"
        if s["delivered_date"]:
            assert s["is_on_time"] == (s["delivered_date"] <= s["promised_date"])
        else:
            assert s["status"] == "in_transit" and s["is_on_time"] is None
    tickets = stage_b["support_tickets"]
    assert sum(t["category"] == "Returns" for t in tickets) == len(returns)
    assert all(t["csat_score"] is None or 1 <= t["csat_score"] <= 5 for t in tickets)
    assert all(t["status"] == "closed" or t["csat_score"] is None for t in tickets)

    # The dashboard look-alike equals gross sales (before cancellations and refunds).
    gross = round(sum(x.quantity * x.unit_price for x in lines if not x.is_cancellation), 2)
    dashboard = round(sum(r["dashboard_revenue"] for r in stage_b["sales_daily_summary"]), 2)
    assert dashboard == gross


def test_stage_b_people_and_finance_edge_cases(stage_b):
    staff = stage_b["staff"]
    assert sum(not s["is_active"] for s in staff) == 3, "three leavers"
    berlin = [s for s in staff if s["store_id"] == "S04"]
    assert any(s["end_date"] for s in berlin) and any(s["start_date"].year == 2026 for s in berlin)
    active = {s["employee_id"]: s for s in staff}
    for shift in stage_b["shifts"]:
        person = active[shift["employee_id"]]
        assert person["start_date"] <= shift["shift_date"]
        assert person["end_date"] is None or shift["shift_date"] <= person["end_date"]
        assert (shift["worked_hours"] == 0) == (shift["absence_reason"] is not None)
    april = [b for b in stage_b["budgets"] if b["budget_month"].month == 4]
    assert april and all(b["actual_eur"] is None for b in april), "April isn't closed"
    assert all(
        b["actual_eur"] is not None for b in stage_b["budgets"] if b["budget_month"].month < 4
    )
    promos = {p["promo_code"]: p for p in stage_b["promotions"]}
    assert promos[PROMO_CODE]["category"] == PROMO_CATEGORY
