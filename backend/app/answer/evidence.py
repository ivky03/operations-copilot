"""Evidence: the computed results an answer may use, plus code-computed comparisons.

Each query result becomes an evidence item with an ID (D1, D2, ...) that the answer must cite.
When a result covers exactly two periods, the differences and percentage changes are computed
here, in code, so the language model never has to do arithmetic.
"""

from dataclasses import dataclass, field

from app.query.compiler import CompiledQuery
from app.query.executor import QueryResult
from app.query.plan import QueryPlan


@dataclass
class EvidenceItem:
    id: str
    metric: str
    title: str
    unit: str | None
    description: str
    columns: list[str]
    rows: list[dict]
    sql: str
    sources: list[dict]
    comparisons: list[dict] = field(default_factory=list)
    definition: str = ""  # the metric's governed definition, from its manifest

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def describe(plan: QueryPlan) -> str:
    parts = [plan.metric]
    groups = ([plan.time_grain] if plan.time_grain else []) + plan.dimensions
    if groups:
        parts.append("by " + ", ".join(groups))
    for f in plan.filters:
        parts.append(f"where {f.dimension} {f.op} {', '.join(f.values)}")
    if plan.time_range:
        parts.append(f"from {plan.time_range.start} up to (not including) {plan.time_range.end}")
    if plan.limit:
        parts.append(f"top {plan.limit}")
    return "; ".join(parts)


def _comparisons(plan: QueryPlan, result: QueryResult, metric: str) -> list[dict]:
    if not plan.time_grain:
        return []
    periods = sorted({row[plan.time_grain] for row in result.rows})
    if len(periods) != 2:
        return []
    earlier, later = periods
    groups: dict[tuple, dict] = {}
    for row in result.rows:
        key = tuple(row[d] for d in plan.dimensions)
        groups.setdefault(key, {})[row[plan.time_grain]] = row[metric]
    out = []
    for key, values in sorted(groups.items(), key=lambda kv: [str(k) for k in kv[0]]):
        a, b = values.get(earlier), values.get(later)
        a0, b0 = (a or 0.0), (b or 0.0)  # a missing period means no data: 0
        change = round(b0 - a0, 2)
        pct = round(change / abs(a0) * 100, 1) if a0 else None
        out.append(
            {
                **dict(zip(plan.dimensions, key, strict=True)),
                "from_period": earlier,
                "to_period": later,
                "from_value": a,
                "to_value": b,
                "change": change,
                "change_pct": pct,  # None when the earlier value was 0 or missing
            }
        )
    return out


def build_evidence(
    index: int, plan: QueryPlan, compiled: CompiledQuery, result: QueryResult
) -> EvidenceItem:
    return EvidenceItem(
        id=f"D{index}",
        metric=compiled.metric,
        title=compiled.title,
        unit=compiled.unit,
        description=describe(plan),
        columns=result.columns,
        rows=result.rows,
        sql=compiled.sql,
        sources=[{"source": s.slug, "version": s.version} for s in compiled.sources],
        comparisons=_comparisons(plan, result, compiled.metric),
        definition=compiled.definition,
    )
