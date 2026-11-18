"""The oracle is checked against a tiny dataset whose answers were worked out by hand.

Who tests the tester? These numbers were calculated by hand from the metric definitions
document, not by running the oracle.
"""

from pathlib import Path

import pytest

from evals.oracle import Oracle, Query

STORES = """store_id,store_name,country,region,channel,opened_date
S1,Alpha Store,Germany,Central Europe,Retail,2020-01-01
S2,Beta Online,France,Southwest Europe,Online,2020-01-01
"""
PRODUCTS = """product_id,sku,product_name,category,list_price,status,discontinued_date
P1,A-1,Lamp,Lighting,10.00,active,
P2,B-1,Mug,Kitchen,5.00,active,
"""
# Feb: order 1 (S1) = 2x10 + 1x5 = 25; order 2 (S2) = 4x5 = 20, cancelled on 1 Mar.
# Mar: order 3 (S1) = 3x10 = 30.
TRANSACTIONS = """line_id,invoice_no,line_no,invoice_date,store_id,product_id,customer_id,quantity,unit_price,promo_code,is_cancellation
INV-1-1,INV-1,1,2026-02-10T10:00:00,S1,P1,C1,2,10.00,,false
INV-1-2,INV-1,2,2026-02-10T10:00:00,S1,P2,C1,1,5.00,,false
INV-2-1,INV-2,1,2026-02-28T20:00:00,S2,P2,,4,5.00,,false
CINV-2-1,CINV-2,1,2026-03-01T09:00:00,S2,P2,,-4,5.00,,true
INV-3-1,INV-3,1,2026-03-05T12:00:00,S1,P1,C2,3,10.00,,false
"""
# One lamp from order 1 refunded in March.
RETURNS = """return_id,invoice_no,line_no,return_date,store_id,product_id,quantity,refund_amount,reason
R-1,INV-1,1,2026-03-02T10:00:00,S1,P1,1,10.00,
"""


@pytest.fixture
def oracle(tmp_path: Path) -> Oracle:
    for name, body in [
        ("stores", STORES),
        ("products", PRODUCTS),
        ("transactions", TRANSACTIONS),
        ("returns", RETURNS),
    ]:
        (tmp_path / f"{name}.csv").write_text(body)
    return Oracle(tmp_path)


def by(rows: list[dict], key: str, metric: str) -> dict:
    return {r[key]: r[metric] for r in rows}


def test_gross_sales_by_month(oracle):
    rows = oracle.run(Query("gross_sales", ["month"]))
    assert by(rows, "month", "gross_sales") == {"2026-02": 45.0, "2026-03": 30.0}


def test_cancellation_counts_in_the_month_it_happened(oracle):
    rows = oracle.run(Query("cancellation_value", ["month"]))
    assert by(rows, "month", "cancellation_value") == {"2026-03": 20.0}


def test_net_sales_attributes_each_component_to_its_own_month(oracle):
    # Feb: 45 - 0 - 0 = 45.   Mar: 30 - 20 - 10 = 0.
    rows = oracle.run(Query("net_sales", ["month"]))
    assert by(rows, "month", "net_sales") == {"2026-02": 45.0, "2026-03": 0.0}


def test_net_sales_can_be_negative_for_a_slice(oracle):
    # France: Feb +20 (sale), Mar -20 (cancellation, no new sales).
    rows = oracle.run(Query("net_sales", ["month"], {"country": ["France"]}))
    assert by(rows, "month", "net_sales") == {"2026-02": 20.0, "2026-03": -20.0}


def test_orders_include_later_cancelled_ones_and_aov(oracle):
    assert oracle.run(Query("order_count", filters={"month": ["2026-02"]})) == [{"order_count": 2}]
    # AOV Feb = 45 / 2 = 22.5
    assert oracle.run(Query("average_order_value", filters={"month": ["2026-02"]})) == [
        {"average_order_value": 22.5}
    ]


def test_cancellation_rate(oracle):
    # 1 cancelled order / 3 orders over the whole period.
    assert oracle.run(Query("cancellation_rate")) == [{"cancellation_rate": 0.3333}]


def test_filters_and_top_n(oracle):
    rows = oracle.run(Query("gross_sales", ["product_name"], order_by_value=True, limit=1))
    assert rows == [{"product_name": "Lamp", "gross_sales": 50.0}]
    rows = oracle.run(Query("units_sold", filters={"channel": ["Online"]}))
    assert rows == [{"units_sold": 4}]


def test_unknown_metric_or_dimension_is_rejected():
    with pytest.raises(ValueError):
        Query("profit_margin")
    with pytest.raises(ValueError):
        Query("gross_sales", ["web_visitors"])
