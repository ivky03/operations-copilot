"""The query plan: a structured, validated description of a data question.

The language model (M3b) produces one of these instead of SQL. Everything in it is checked
against the catalog of datasets the user may read, then compiled to SQL by our own code.

Example, "net sales by country for February and March 2026":
    {"metric": "net_sales", "dimensions": ["country"],
     "time_range": {"start": "2026-02-01", "end": "2026-04-01"}, "time_grain": "month"}
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NAME = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$"  # "country" or "stores.country"
MAX_LIMIT = 1000
DimensionName = Annotated[str, StringConstraints(pattern=NAME)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Filter(_Strict):
    dimension: str = Field(pattern=NAME)
    op: Literal["=", "!=", "in", "not in"] = "="
    values: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _single_value_ops(self) -> "Filter":
        if self.op in {"=", "!="} and len(self.values) != 1:
            raise ValueError(f"operator {self.op!r} takes exactly one value; use 'in' for several")
        return self


class TimeRange(_Strict):
    """Half-open range: start <= time < end. "February 2026" is 2026-02-01 to 2026-03-01."""

    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> "TimeRange":
        if self.end <= self.start:
            raise ValueError("time_range end must be after start")
        return self


class QueryPlan(_Strict):
    metric: str = Field(pattern=NAME)
    dimensions: list[DimensionName] = Field(default=[], max_length=4)
    filters: list[Filter] = Field(default=[], max_length=10)
    time_range: TimeRange | None = None
    time_grain: Literal["day", "week", "month", "quarter", "year"] | None = None
    order: Literal["dimensions", "metric_desc", "metric_asc"] = "dimensions"
    limit: int | None = Field(default=None, ge=1, le=MAX_LIMIT)

    @model_validator(mode="after")
    def _distinct(self) -> "QueryPlan":
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError("dimensions must not repeat")
        return self
