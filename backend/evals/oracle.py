"""Independent answer key ("oracle") for numeric evaluation questions.

The copilot computes answers by compiling a query plan to SQL. This module computes the same
metrics a completely different way: plain pandas over the CSV files, written from the metric
definitions (the Metric Definitions document for Stage A, the manifests' plain-English
descriptions for Stage B), sharing no code with the application. If both agree, we have real
evidence the copilot is correct. A bug would have to be made identically twice to hide.

Stage A: the four sales tables. Stage B: Stage A unchanged plus eleven operational tables.
Stage C: Stage B plus 35 more; only the metrics its evaluation set asks about are written here.

Dimensions: month (YYYY-MM) and day (YYYY-MM-DD) of the metric's own date column, plus any
column of the metric's table or of a table it links to (stores, products, suppliers, staff).
The metric's own table wins a name clash: a support ticket's "channel" is email/chat/phone,
not the store's channel. A name found only on two linked tables is ambiguous and rejected.
Filters: {"month": ["2026-02", ...], "<dimension>": [values]}.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2] / "data"
DEFAULT_DATA = ROOT / "reference" / "tables"
STAGE_B_DATA = ROOT / "stage_b" / "tables"
STAGE_C_DATA = ROOT / "stage_c" / "tables"

# ---------------------------------------------------------------------------- Stage A
CURRENCY_METRICS = {
    "gross_sales",
    "cancellation_value",
    "refund_value",
    "net_sales",
    "average_order_value",
}
BASE_METRICS = {
    "gross_sales",
    "cancellation_value",
    "units_sold",
    "order_count",
    "cancelled_order_count",
    "refund_value",
    "units_returned",
}
DERIVED_METRICS = {
    "net_sales": ("gross_sales", "cancellation_value", "refund_value"),
    "average_order_value": ("gross_sales", "order_count"),
    "cancellation_rate": ("cancelled_order_count", "order_count"),
}
STAGE_A_METRICS = BASE_METRICS | set(DERIVED_METRICS)
STAGE_A_DIMENSIONS = {
    "month",
    "country",
    "region",
    "channel",
    "store_name",
    "category",
    "product_name",
}

# ---------------------------------------------------------------------------- Stage B
# metric -> (table, which rows count, column, how). Written from each metric's description.
# how: sum | count (non-empty values, like SQL COUNT) | nunique | mean.
# Sums over only empty values stay empty (April's actual cost is unknown, not zero).
_ALL = None
STAGE_B_SPECS = {
    # Purchasing
    "purchase_value": ("purchase_orders", _ALL, "_purchase_value", "sum"),
    "units_ordered": ("purchase_orders", _ALL, "quantity_ordered", "sum"),
    "units_received": ("purchase_orders", _ALL, "quantity_received", "sum"),
    "purchase_order_count": ("purchase_orders", _ALL, "po_number", "nunique"),
    "received_purchase_order_count": (
        "purchase_orders",
        lambda d: ~d["status"].isin(["open", "cancelled"]),
        "po_number",
        "nunique",
    ),
    "late_purchase_order_count": (
        "purchase_orders",
        lambda d: d["days_late"] > 0,
        "po_number",
        "nunique",
    ),
    "open_purchase_order_count": (
        "purchase_orders",
        lambda d: d["status"] == "open",
        "po_number",
        "nunique",
    ),
    "supplier_count": ("suppliers", _ALL, "supplier_id", "nunique"),
    "average_lead_time_days": ("suppliers", _ALL, "lead_time_days", "mean"),
    # Stock (point in time: filter to one day)
    "units_on_hand": ("inventory_snapshots", _ALL, "units_on_hand", "sum"),
    "units_on_order": ("inventory_snapshots", _ALL, "units_on_order", "sum"),
    "stockout_days": (
        "inventory_snapshots",
        lambda d: d["units_on_hand"] == 0,
        "product_id",
        "count",
    ),
    # Promotions
    "promotion_count": ("promotions", _ALL, "promo_code", "nunique"),
    "average_discount_pct": ("promotions", _ALL, "discount_pct", "mean"),
    # People
    "headcount": ("staff", lambda d: d["end_date"].isna(), "employee_id", "nunique"),
    "employees_any_time": ("staff", _ALL, "employee_id", "nunique"),
    "contracted_hours": ("staff", lambda d: d["end_date"].isna(), "weekly_contract_hours", "sum"),
    "shift_count": ("shifts", _ALL, "shift_id", "count"),
    "hours_scheduled": ("shifts", _ALL, "scheduled_hours", "sum"),
    "hours_worked": ("shifts", _ALL, "worked_hours", "sum"),
    "absence_count": ("shifts", lambda d: d["worked_hours"] == 0, "shift_id", "count"),
    "sick_shift_count": ("shifts", lambda d: d["absence_reason"] == "sick", "shift_id", "count"),
    # Deliveries
    "shipment_count": ("shipments", _ALL, "shipment_id", "count"),
    "delivered_count": ("shipments", lambda d: d["delivered_date"].notna(), "shipment_id", "count"),
    "on_time_count": (
        "shipments",
        lambda d: d["delivered_date"].notna() & (d["delivered_date"] <= d["promised_date"]),
        "shipment_id",
        "count",
    ),
    "average_delivery_days": ("shipments", _ALL, "delivery_days", "mean"),
    # Customer service
    "ticket_count": ("support_tickets", _ALL, "ticket_id", "count"),
    "open_ticket_count": ("support_tickets", lambda d: d["closed_at"].isna(), "ticket_id", "count"),
    "average_first_response_minutes": ("support_tickets", _ALL, "first_response_minutes", "mean"),
    "average_resolution_hours": ("support_tickets", _ALL, "resolution_hours", "mean"),
    "average_csat": ("support_tickets", _ALL, "csat_score", "mean"),
    "csat_responses": ("support_tickets", _ALL, "csat_score", "count"),
    # Dashboards, plans and budgets
    "dashboard_revenue": ("sales_daily_summary", _ALL, "dashboard_revenue", "sum"),
    "dashboard_orders": ("sales_daily_summary", _ALL, "orders_placed", "sum"),
    "net_sales_target": ("sales_targets", _ALL, "net_sales_target", "sum"),
    "budgeted_cost": ("budgets", _ALL, "budget_eur", "sum"),
    "actual_cost": ("budgets", _ALL, "actual_eur", "sum"),
}
STAGE_B_DERIVED = {
    "absence_rate": ("absence_count", "shift_count"),
    "on_time_delivery_rate": ("on_time_count", "delivered_count"),
    "supplier_late_rate": ("late_purchase_order_count", "received_purchase_order_count"),
}
STAGE_B_DATE = {
    "purchase_orders": "ordered_date",
    "inventory_snapshots": "snapshot_date",
    "promotions": "start_date",
    "staff": "start_date",
    "shifts": "shift_date",
    "shipments": "shipped_date",
    "support_tickets": "opened_at",
    "sales_daily_summary": "summary_date",
    "sales_targets": "target_month",
    "budgets": "budget_month",
}
# ---------------------------------------------------------------------------- Stage C
# Same shape as Stage B, written from each metric's description in datagen/stage_c.py.
STAGE_C_SPECS = {
    # Marketing and web
    "media_spend": ("campaign_spend_daily", _ALL, "spend", "sum"),
    "attributed_revenue": ("campaign_spend_daily", _ALL, "attributed_revenue", "sum"),
    "sessions": ("web_sessions_daily", _ALL, "sessions", "sum"),
    "web_orders": ("web_sessions_daily", _ALL, "orders", "sum"),
    "online_revenue": ("web_sessions_daily", _ALL, "online_revenue", "sum"),
    "emails_delivered": ("email_sends", _ALL, "delivered", "sum"),
    "email_opens": ("email_sends", _ALL, "opens", "sum"),
    "zero_result_searches": ("site_searches", lambda d: ~d["results_found"], "searches", "sum"),
    "page_views": ("product_page_views", _ALL, "page_views", "sum"),
    # Customers and loyalty
    "customer_count": ("customers", _ALL, "customer_id", "count"),
    "opted_in_customer_count": (
        "customers",
        lambda d: d["marketing_opt_in"],
        "customer_id",
        "count",
    ),
    "member_count": ("loyalty_members", _ALL, "member_id", "count"),
    "points_redeemed": ("loyalty_points", lambda d: d["entry_type"] == "redeem", "points", "sum"),
    "gift_card_liability": ("gift_cards", _ALL, "balance", "sum"),
    "nps_response_count": ("nps_responses", _ALL, "response_id", "count"),
    "promoter_count": ("nps_responses", lambda d: d["score"] >= 9, "response_id", "count"),
    "detractor_count": ("nps_responses", lambda d: d["score"] <= 6, "response_id", "count"),
    # Catalogue
    "average_landed_cost": ("product_costs", _ALL, "landed_cost", "mean"),
    "average_rating": ("product_reviews", _ALL, "rating", "mean"),
    "price_change_count": ("price_changes", _ALL, "change_id", "count"),
    # Store operations
    "visitors": ("store_footfall_daily", _ALL, "visitors", "sum"),
    "energy_cost": ("energy_usage", _ALL, "energy_cost", "sum"),
    "incident_count": ("store_incidents", _ALL, "incident_id", "count"),
    "maintenance_cost": ("maintenance_requests", _ALL, "cost", "sum"),
    "failed_audit_count": ("store_audits", lambda d: ~d["passed"], "audit_id", "count"),
    # Warehousing (stock is point in time: filter to one week)
    "warehouse_units_available": ("warehouse_stock", _ALL, "units_available", "sum"),
    "transfer_count": ("stock_transfers", _ALL, "transfer_id", "count"),
    "write_off_value": ("return_inspections", _ALL, "write_off_value", "sum"),
    # People
    "training_overdue": (
        "training_records",
        lambda d: d["status"] == "overdue",
        "record_id",
        "count",
    ),
    "open_vacancy_count": ("vacancies", lambda d: d["status"] == "open", "vacancy_id", "count"),
    "average_engagement": ("staff_survey", _ALL, "engagement_score", "mean"),
    "overtime_pay": ("payroll", _ALL, "overtime_pay", "sum"),
    # Finance
    "accounting_revenue": (
        "gl_entries",
        lambda d: d["account_code"].astype(str) == "4000",
        "amount",
        "sum",
    ),
    "unpaid_invoice_amount": (
        "supplier_invoices",
        lambda d: d["status"].isin(["unpaid", "disputed"]),
        "amount",
        "sum",
    ),
    "expenses_approved": ("expenses", lambda d: d["approved"], "amount", "sum"),
}
STAGE_C_DERIVED = {
    "conversion_rate": ("web_orders", "sessions"),
    "email_open_rate": ("email_opens", "emails_delivered"),
}
# (promoters - detractors) / responses: three parts, so written out on its own.
NPS = ("promoter_count", "detractor_count", "nps_response_count")
STAGE_C_DATE = {
    "campaign_spend_daily": "spend_date",
    "web_sessions_daily": "session_date",
    "email_sends": "send_date",
    "site_searches": "search_date",
    "product_page_views": "view_date",
    "customers": "signup_date",
    "loyalty_members": "joined_date",
    "loyalty_points": "entry_date",
    "gift_cards": "issued_date",
    "nps_responses": "response_date",
    "product_costs": "cost_month",
    "product_reviews": "review_date",
    "price_changes": "effective_date",
    "store_footfall_daily": "footfall_date",
    "energy_usage": "usage_month",
    "store_incidents": "incident_date",
    "maintenance_requests": "opened_date",
    "store_audits": "audit_date",
    "warehouse_stock": "snapshot_week",
    "stock_transfers": "requested_date",
    "return_inspections": "inspected_date",
    "training_records": "due_date",
    "vacancies": "opened_date",
    "staff_survey": "survey_month",
    "payroll": "pay_month",
    "gl_entries": "posting_date",
    "supplier_invoices": "invoice_date",
    "expenses": "expense_date",
}
SPECS = STAGE_B_SPECS | STAGE_C_SPECS
DERIVED = STAGE_B_DERIVED | STAGE_C_DERIVED
DATE_COLUMN = STAGE_B_DATE | STAGE_C_DATE

# key column -> the table it links to (the declared relationships)
LINKS = {
    "store_id": "stores",
    "product_id": "products",
    "supplier_id": "suppliers",
    "employee_id": "staff",
    "campaign_id": "marketing_campaigns",
    "warehouse_id": "warehouses",
}

RATIO_METRICS = {"cancellation_rate", *DERIVED, "net_promoter_score"}
COUNT_METRICS = {
    "units_sold",
    "order_count",
    "cancelled_order_count",
    "units_returned",
    *(m for m, (_, _, _, how) in SPECS.items() if how in {"count", "nunique"}),
    "units_ordered",
    "units_received",
    "units_on_hand",
    "units_on_order",
    "dashboard_orders",
    "sessions",
    "web_orders",
    "emails_delivered",
    "email_opens",
    "zero_result_searches",
    "page_views",
    "points_redeemed",
    "visitors",
    "warehouse_units_available",
}
METRICS = STAGE_A_METRICS | set(SPECS) | set(DERIVED) | {"net_promoter_score"}


def _half_up(value, places: str) -> float:
    """Round half up after removing float noise, so an exact tie (6.765) always rounds the same
    way however the mean was computed."""
    return float(Decimal(str(round(float(value), 6))).quantize(Decimal(places), ROUND_HALF_UP))


@dataclass
class Query:
    metric: str
    group_by: list[str] = field(default_factory=list)
    filters: dict[str, list[str]] = field(default_factory=dict)
    order_by_value: bool = False  # sort by the metric, descending
    limit: int | None = None

    def __post_init__(self) -> None:
        if self.metric not in METRICS:
            raise ValueError(f"unknown metric {self.metric!r}")
        if self.metric in STAGE_A_METRICS:
            unknown = (set(self.group_by) | set(self.filters)) - STAGE_A_DIMENSIONS
            if unknown:
                raise ValueError(f"unknown dimensions {sorted(unknown)}")


class Oracle:
    def __init__(
        self,
        data_dir: Path = DEFAULT_DATA,
        stage_b_dir: Path | None = None,
        stage_c_dir: Path | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.stage_b_dir = stage_b_dir
        self.stage_c_dir = stage_c_dir

    @classmethod
    def for_stage(cls, stage: str) -> "Oracle":
        if stage == "stage_c":
            return cls(DEFAULT_DATA, STAGE_B_DATA, STAGE_C_DATA)
        return cls(DEFAULT_DATA, STAGE_B_DATA if stage == "stage_b" else None)

    # ------------------------------------------------------------------ Stage A (unchanged)
    @cached_property
    def _facts(self) -> dict[str, pd.DataFrame]:
        d = self.data_dir
        stores = pd.read_csv(d / "stores.csv")
        products = pd.read_csv(d / "products.csv")
        tx = pd.read_csv(d / "transactions.csv", parse_dates=["invoice_date"])
        rt = pd.read_csv(d / "returns.csv", parse_dates=["return_date"])

        def enrich(df: pd.DataFrame, time_col: str) -> pd.DataFrame:
            df = df.merge(stores, on="store_id", how="left", validate="many_to_one")
            df = df.merge(products, on="product_id", how="left", validate="many_to_one")
            df["month"] = df[time_col].dt.strftime("%Y-%m")
            return df

        tx = enrich(tx, "invoice_date")
        tx["line_value"] = tx["quantity"] * tx["unit_price"]
        return {"transactions": tx, "returns": enrich(rt, "return_date")}

    def _base(self, metric: str, q: Query) -> pd.Series:
        tx, rt = self._facts["transactions"], self._facts["returns"]
        sales, cancels = tx[~tx["is_cancellation"]], tx[tx["is_cancellation"]]
        frame, column, how = {
            "gross_sales": (sales, "line_value", "sum"),
            "cancellation_value": (cancels, "line_value", "neg_sum"),
            "units_sold": (sales, "quantity", "sum"),
            "order_count": (sales, "invoice_no", "nunique"),
            "cancelled_order_count": (cancels, "invoice_no", "nunique"),
            "refund_value": (rt, "refund_amount", "sum"),
            "units_returned": (rt, "quantity", "sum"),
        }[metric]
        for dim, values in q.filters.items():
            frame = frame[frame[dim].isin(values)]
        if q.group_by:
            grouped = frame.groupby(q.group_by)[column]
        else:  # one total row
            grouped = frame.assign(_all="all").groupby("_all")[column]
        if how == "nunique":
            return grouped.nunique()
        total = grouped.sum()
        return -total if how == "neg_sum" else total

    # ------------------------------------------------------------------ Stage B
    @cached_property
    def _stage_b_tables(self) -> dict[str, pd.DataFrame]:
        if self.stage_b_dir is None:
            raise ValueError("this oracle has no Stage B data (use Oracle.for_stage('stage_b'))")
        dirs = [self.stage_b_dir] + ([self.stage_c_dir] if self.stage_c_dir else [])
        tables = {p.stem: pd.read_csv(p) for d in dirs for p in sorted(d.glob("*.csv"))}
        tables["stores"] = pd.read_csv(self.data_dir / "stores.csv")
        tables["products"] = pd.read_csv(self.data_dir / "products.csv")
        for df in tables.values():
            for col in ("end_date", "promised_date", "delivered_date", "closed_at"):
                if col in df:
                    df[col] = pd.to_datetime(df[col])
        po = tables["purchase_orders"]
        po["_purchase_value"] = po["quantity_ordered"] * po["unit_cost"]
        return tables

    def _stage_b_frame(self, table: str, needed: set[str]) -> pd.DataFrame:
        tables = self._stage_b_tables
        df = tables[table].copy()
        if table not in tables:
            raise ValueError(f"no data for table {table!r} in this stage")
        if table in DATE_COLUMN:
            when = pd.to_datetime(df[DATE_COLUMN[table]])
            df["month"], df["day"] = when.dt.strftime("%Y-%m"), when.dt.strftime("%Y-%m-%d")
        own = set(df.columns)
        linked = {key: LINKS[key] for key in LINKS if key in own and LINKS[key] != table}
        for dim in sorted(needed - own):
            holders = [t for t in linked.values() if dim in tables[t].columns]
            if len(holders) != 1:
                raise ValueError(f"dimension {dim!r} for {table}: found on {holders or 'nothing'}")
            key = next(k for k, t in linked.items() if t == holders[0])
            lookup = tables[holders[0]][[key, dim]]
            df = df.merge(lookup, on=key, how="left", validate="many_to_one")
        return df

    def _stage_b_base(self, metric: str, q: Query) -> pd.Series:
        table, rows, column, how = SPECS[metric]
        frame = self._stage_b_frame(table, set(q.group_by) | set(q.filters))
        if rows is not None:
            frame = frame[rows(frame)]
        for dim, values in q.filters.items():
            frame = frame[frame[dim].astype(str).isin([str(v) for v in values])]
        if q.group_by:  # like SQL GROUP BY, an empty value is a group of its own
            grouped = frame.groupby(q.group_by, dropna=False)[column]
        else:
            grouped = frame.assign(_all="all").groupby("_all")[column]
        if how == "sum":
            return grouped.sum(min_count=1)
        return {"count": grouped.count, "nunique": grouped.nunique, "mean": grouped.mean}[how]()

    # ------------------------------------------------------------------ both
    def run(self, q: Query) -> list[dict]:
        if q.metric in DERIVED_METRICS:
            parts = {m: self._base(m, q) for m in DERIVED_METRICS[q.metric]}
            joined = pd.concat(parts, axis=1)  # outer join on the group keys
            if q.metric == "net_sales":
                filled = joined.fillna(0)
                value = (
                    filled["gross_sales"] - filled["cancellation_value"] - filled["refund_value"]
                )
            else:
                num, den = DERIVED_METRICS[q.metric]
                value = joined[num].fillna(0) / joined[den]
        elif q.metric in BASE_METRICS:
            value = self._base(q.metric, q)
        elif q.metric == "net_promoter_score":
            pro, det, n = NPS
            joined = pd.concat({m: self._stage_b_base(m, q) for m in NPS}, axis=1)
            filled = joined[[pro, det]].fillna(0)
            value = ((filled[pro] - filled[det]) / joined[n]).replace([np.inf, -np.inf], np.nan)
        elif q.metric in DERIVED:
            num, den = DERIVED[q.metric]
            joined = pd.concat({m: self._stage_b_base(m, q) for m in (num, den)}, axis=1)
            value = (joined[num].fillna(0) / joined[den]).replace([np.inf, -np.inf], np.nan)
        else:
            value = self._stage_b_base(q.metric, q)

        df = value.rename(q.metric).reset_index()
        df = df.drop(columns=["_all"], errors="ignore")
        if q.order_by_value:
            df = df.sort_values(q.metric, ascending=False, kind="stable")
        else:
            df = df.sort_values(q.group_by, kind="stable") if q.group_by else df
        if q.limit:
            df = df.head(q.limit)
        return [self._clean(row, q.metric) for row in df.to_dict("records")]

    @staticmethod
    def _clean(row: dict, metric: str) -> dict:
        value = row[metric]
        if value is None or pd.isna(value):
            row[metric] = None
        elif metric in RATIO_METRICS:
            row[metric] = _half_up(value, "0.0001")
        elif metric in COUNT_METRICS:
            row[metric] = int(value)
        else:  # currency, hours, days, averages
            row[metric] = _half_up(value, "0.01")

        def plain(v):
            if isinstance(v, float) and np.isnan(v):
                return None  # an empty group key (e.g. central staff have no store)
            return v.item() if hasattr(v, "item") else v

        return {k: plain(v) for k, v in row.items()}
