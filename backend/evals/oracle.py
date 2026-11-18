"""Independent answer key ("oracle") for numeric evaluation questions.

The copilot computes answers by compiling a query plan to SQL. This module computes the same
metrics a completely different way: plain pandas over the CSV files, written from the metric
definitions document, sharing no code with the application. If both agree, we have real
evidence the copilot is correct. A bug would have to be made identically twice to hide.

Supported dimensions: month (YYYY-MM), country, region, channel, store_name, category,
product_name. Filters: {"month": ["2026-02", ...], "<dimension>": [values]}.
"""

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import pandas as pd

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data" / "reference" / "tables"

DIMENSIONS = {"month", "country", "region", "channel", "store_name", "category", "product_name"}
CURRENCY_METRICS = {
    "gross_sales",
    "cancellation_value",
    "refund_value",
    "net_sales",
    "average_order_value",
}
RATIO_METRICS = {"cancellation_rate"}
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
METRICS = BASE_METRICS | set(DERIVED_METRICS)


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
        unknown = (set(self.group_by) | set(self.filters)) - DIMENSIONS
        if unknown:
            raise ValueError(f"unknown dimensions {sorted(unknown)}")


class Oracle:
    def __init__(self, data_dir: Path = DEFAULT_DATA) -> None:
        self.data_dir = data_dir

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
        else:
            value = self._base(q.metric, q)

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
        if metric in CURRENCY_METRICS:
            row[metric] = round(float(value), 2)
        elif metric in RATIO_METRICS:
            row[metric] = round(float(value), 4)
        else:
            row[metric] = int(value)
        return {k: (v if not hasattr(v, "item") else v.item()) for k, v in row.items()}
