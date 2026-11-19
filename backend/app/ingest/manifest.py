"""Dataset manifest schema and validation.

A manifest describes a table: its columns, how it links to other tables, value aliases and
governed metric definitions. It is written by a data steward, so it is untrusted input:
- column names become real database column names, so they must be safe identifiers;
- metric expressions are parsed by the safe grammar in expressions.py, never used as SQL;
- unknown keys are rejected (extra="forbid") so a typo can't silently do nothing.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.ingest.expressions import ExpressionError, names, parse

# Safe as an unquoted Postgres identifier, and short enough (Postgres limit is 63 bytes).
IDENTIFIER = r"^[a-z][a-z0-9_]{0,62}$"
SLUG = r"^[a-z][a-z0-9-]{0,99}$"
MAX_COLUMNS = 200

ColumnType = Literal["string", "integer", "decimal", "boolean", "date", "timestamp"]
NUMERIC_TYPES = {"integer", "decimal"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Column(_Strict):
    name: str = Field(pattern=IDENTIFIER)
    type: ColumnType
    role: Literal["id", "dimension", "measure", "time", "attribute"]
    description: str = Field(min_length=1)
    nullable: bool = False
    value_index: bool = False
    aliases: dict[str, str] = {}
    allowed_values: list[str] | None = None
    unit: str | None = None


class Relationship(_Strict):
    column: str
    references: str = Field(pattern=r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_]*$")  # source.column

    @property
    def target_source(self) -> str:
        return self.references.split(".")[0]

    @property
    def target_column(self) -> str:
        return self.references.split(".")[1]


class Filter(_Strict):
    column: str
    op: Literal["=", "!=", "<", "<=", ">", ">=", "in"]
    value: str | int | float | bool | list[str | int | float]


class Metric(_Strict):
    name: str = Field(pattern=IDENTIFIER)
    title: str
    description: str
    aggregation: Literal["sum", "count", "count_distinct", "avg", "min", "max"]
    expression: str
    filters: list[Filter] = []
    unit: str | None = None


class DerivedMetric(_Strict):
    name: str = Field(pattern=IDENTIFIER)
    title: str
    description: str
    formula: str  # other metrics, e.g. "gross_sales - cancellation_value - returns.refund_value"
    unit: str | None = None


class Manifest(_Strict):
    source: str = Field(pattern=SLUG)
    title: str
    description: str
    grain: str
    primary_key: list[str] = Field(min_length=1)
    time_column: str | None = None
    columns: list[Column] = Field(min_length=1, max_length=MAX_COLUMNS)
    relationships: list[Relationship] = []
    metrics: list[Metric] = []
    derived_metrics: list[DerivedMetric] = []

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def metric_names(self) -> set[str]:
        return {m.name for m in self.metrics} | {d.name for d in self.derived_metrics}

    def external_metric_refs(self) -> set[str]:
        """'source.metric' references to other datasets made by derived formulas."""
        return {n for d in self.derived_metrics for n in names(parse(d.formula)) if "." in n}

    @model_validator(mode="after")
    def _check_internal_consistency(self) -> "Manifest":
        problems: list[str] = []
        column_names = [c.name for c in self.columns]
        if len(column_names) != len(set(column_names)):
            problems.append("column names must be unique")
        for key in self.primary_key:
            if key not in column_names:
                problems.append(f"primary_key column {key!r} is not defined")
        if self.time_column:
            col = self.column(self.time_column)
            if col is None or col.type not in {"date", "timestamp"}:
                problems.append("time_column must be a date or timestamp column")
        for col in self.columns:
            if col.aliases and not col.value_index:
                problems.append(f"column {col.name!r}: aliases require value_index: true")
            if col.allowed_values is not None and col.type != "string":
                problems.append(f"column {col.name!r}: allowed_values only apply to strings")
        for rel in self.relationships:
            if rel.column not in column_names:
                problems.append(f"relationship column {rel.column!r} is not defined")

        all_metrics = [m.name for m in self.metrics] + [d.name for d in self.derived_metrics]
        if len(all_metrics) != len(set(all_metrics)):
            problems.append("metric names must be unique")
        for metric in self.metrics:
            problems += self._check_metric(metric)
        local = {m.name for m in self.metrics}
        for derived in self.derived_metrics:
            try:
                refs = names(parse(derived.formula))
            except ExpressionError as e:
                problems.append(f"derived metric {derived.name!r}: {e}")
                continue
            for ref in refs:
                if "." not in ref and ref not in local:
                    problems.append(f"derived metric {derived.name!r}: unknown metric {ref!r}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _check_metric(self, metric: Metric) -> list[str]:
        problems = []
        where = f"metric {metric.name!r}"
        try:
            refs = names(parse(metric.expression))
        except ExpressionError as e:
            return [f"{where}: {e}"]
        for ref in refs:
            col = self.column(ref)
            if col is None:
                problems.append(f"{where}: unknown column {ref!r}")
            elif metric.aggregation in {"sum", "avg"} and col.type not in NUMERIC_TYPES:
                problems.append(f"{where}: can't {metric.aggregation} non-numeric column {ref!r}")
        for f in metric.filters:
            if self.column(f.column) is None:
                problems.append(f"{where}: filter on unknown column {f.column!r}")
        return problems


def parse_manifest(data: object) -> tuple[Manifest | None, list[str]]:
    """Validate raw manifest data. Returns (manifest, []) or (None, readable problems)."""
    try:
        return Manifest.model_validate(data), []
    except ValidationError as e:
        problems = []
        for err in e.errors():
            location = ".".join(str(p) for p in err["loc"]) or "manifest"
            message = err["msg"].removeprefix("Value error, ")
            problems.append(f"{location}: {message}")
        return None, problems
