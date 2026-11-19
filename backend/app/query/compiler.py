"""Compile a validated QueryPlan into parameterized SQL.

Safety rules:
- Table and column names come only from the catalog (manifests validated at publish time) and
  are always double-quoted. Nothing from the plan is pasted into SQL as text except names
  that were looked up in the catalog.
- Every value (filter values, dates, limits) is a bound parameter, sent separately from SQL.

Shape of the generated SQL, e.g. net_sales by country:

    WITH c0 AS (SELECT stores.country AS "country", SUM(t.quantity * t.unit_price) AS v
                FROM transactions t LEFT JOIN stores ... WHERE NOT is_cancellation ... GROUP BY 1),
         c1 AS (... cancellation_value ...),
         c2 AS (... returns.refund_value, filtered on the *returns* date column ...)
    SELECT "country", COALESCE(c0.v,0) - COALESCE(c1.v,0) - COALESCE(c2.v,0) AS "net_sales"
    FROM c0 FULL JOIN c1 USING ("country") FULL JOIN c2 USING ("country")

Each component is filtered on its own dataset's time column, so every part of a derived
metric counts in the period it happened (sale date, cancellation date, refund date).
"""

from dataclasses import dataclass, field

from app.ingest import expressions as ex
from app.ingest.manifest import DerivedMetric, Metric
from app.ingest.publish import DATASET_SCHEMA
from app.query.catalog import Catalog, Dataset, PlanError
from app.query.plan import QueryPlan

AGGREGATES = {
    "sum": "SUM({})",
    "avg": "AVG({})",
    "min": "MIN({})",
    "max": "MAX({})",
    "count": "COUNT({})",
    "count_distinct": "COUNT(DISTINCT {})",
}
GRAIN_FORMAT = {
    "day": "YYYY-MM-DD",
    "week": 'IYYY-"W"IW',
    "month": "YYYY-MM",
    "quarter": 'YYYY-"Q"Q',
    "year": "YYYY",
}
FILTER_OPS = {"=": "=", "!=": "<>", "in": "IN", "not in": "NOT IN"}
METRIC_FILTER_OPS = {"=": "=", "!=": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


def q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclass
class CompiledQuery:
    sql: str
    params: dict[str, object]
    columns: list[str]  # output column names, in order
    metric: str
    unit: str | None
    title: str
    sources: list[Dataset]  # dataset versions the answer depends on
    definition: str = ""  # governed description from the manifest


@dataclass
class _Builder:
    params: dict[str, object] = field(default_factory=dict)

    def bind(self, value: object) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = value
        return f":{name}"


def _expr_sql(node: ex.Node, alias: str, dataset: Dataset) -> str:
    """Metric expression AST → SQL, with every column name checked against the manifest."""
    if isinstance(node, ex.Num):
        return repr(node.value)  # a float produced by our parser, not user text
    if isinstance(node, ex.Name):
        if dataset.manifest.column(node.name) is None:
            raise PlanError(f"Metric uses unknown column {node.name!r}.")
        return f"{alias}.{q(node.name)}"
    if isinstance(node, ex.Neg):
        return f"(-{_expr_sql(node.operand, alias, dataset)})"
    if isinstance(node, ex.BinOp):
        left, right = _expr_sql(node.left, alias, dataset), _expr_sql(node.right, alias, dataset)
        return f"({left} {node.op} {right})"
    raise AssertionError(node)


def _component_sql(
    catalog: Catalog, dataset: Dataset, metric: Metric, plan: QueryPlan, b: _Builder
) -> tuple[str, list[str], set[str]]:
    """SELECT <dims>, <aggregate> AS v FROM <dataset> [joins] WHERE ... GROUP BY <dims>.
    Also returns every dataset it touched (joined for a dimension or a filter)."""
    aliases = {dataset.slug: "t0"}
    joins: list[str] = []

    def alias_for(resolved) -> str:
        for join in resolved.path:
            if join.to_slug not in aliases:
                aliases[join.to_slug] = f"t{len(aliases)}"
                target = catalog.datasets[join.to_slug]
                joins.append(
                    f"LEFT JOIN {DATASET_SCHEMA}.{q(target.table)} AS {aliases[join.to_slug]} "
                    f"ON {aliases[join.from_slug]}.{q(join.from_column)} = "
                    f"{aliases[join.to_slug]}.{q(join.to_column)}"
                )
        return aliases[resolved.dataset.slug]

    select_parts, group_names = [], []
    time_col = dataset.manifest.time_column
    if (plan.time_grain or plan.time_range) and not time_col:
        raise PlanError(f"{dataset.slug} data has no date, so it can't be filtered by time.")
    if plan.time_grain:
        select_parts.append(
            f"to_char(date_trunc('{plan.time_grain}', t0.{q(time_col)}), "
            f"'{GRAIN_FORMAT[plan.time_grain]}') AS {q(plan.time_grain)}"
        )
        group_names.append(plan.time_grain)
    for dim in plan.dimensions:
        resolved = catalog.resolve_dimension(dataset, dim)
        select_parts.append(f"{alias_for(resolved)}.{q(resolved.column.name)} AS {q(dim)}")
        group_names.append(dim)

    where: list[str] = []
    for f in metric.filters:  # the metric's governed definition, e.g. NOT is_cancellation
        op = METRIC_FILTER_OPS.get(f.op)
        if f.op == "in":
            where.append(f"t0.{q(f.column)} IN ({', '.join(b.bind(v) for v in f.value)})")
        else:
            where.append(f"t0.{q(f.column)} {op} {b.bind(f.value)}")
    for f in plan.filters:
        resolved = catalog.resolve_dimension(dataset, f.dimension)
        column = f"{alias_for(resolved)}.{q(resolved.column.name)}"
        placeholders = ", ".join(b.bind(v) for v in f.values)
        if f.op in {"=", "!="}:
            where.append(f"{column}::text {FILTER_OPS[f.op]} {placeholders}")
        else:
            where.append(f"{column}::text {FILTER_OPS[f.op]} ({placeholders})")
    if plan.time_range:
        where.append(f"t0.{q(time_col)} >= {b.bind(plan.time_range.start)}")
        where.append(f"t0.{q(time_col)} < {b.bind(plan.time_range.end)}")

    value = AGGREGATES[metric.aggregation].format(
        _expr_sql(ex.parse(metric.expression), "t0", dataset)
    )
    # Reviewed for injection: only catalog identifiers (quoted) and fixed SQL keywords are
    # formatted in; every value is a bound parameter.
    sql = (
        f"SELECT {', '.join([*select_parts, f'{value} AS v'])} "  # noqa: S608
        f"FROM {DATASET_SCHEMA}.{q(dataset.table)} AS t0 "
        + " ".join(joins)
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + (
            f" GROUP BY {', '.join(str(i + 1) for i in range(len(group_names)))}"
            if group_names
            # A SQL total over zero rows still returns one (NULL) row. "No matching data" should
            # be no row, as with grouped results, so a missing component isn't reported as 0.
            else " HAVING COUNT(*) > 0"
        )
    )
    return sql, group_names, set(aliases)


def _formula_sql(node: ex.Node, refs: dict[str, str], denominator: bool = False) -> str:
    """Derived formula AST → SQL over component values. Missing components count as 0;
        division by zero yields NULL (shown as 'not available') instead of an error, and division is
    always done in exact decimals."""
    if isinstance(node, ex.Num):
        return repr(node.value)
    if isinstance(node, ex.Name):
        value = f"COALESCE({refs[node.name]}.v, 0)"
        return f"NULLIF({value}, 0)" if denominator else value
    if isinstance(node, ex.Neg):
        return f"(-{_formula_sql(node.operand, refs, denominator)})"
    if isinstance(node, ex.BinOp):
        left = _formula_sql(node.left, refs)
        right = _formula_sql(node.right, refs, denominator=node.op == "/")
        if node.op == "/":
            # Cast first: in SQL, integer / integer truncates (5 / 95 = 0, not 0.0526).
            return f"({left}::numeric / {right})"
        return f"({left} {node.op} {right})"
    raise AssertionError(node)


def compile_plan(plan: QueryPlan, catalog: Catalog) -> CompiledQuery:
    dataset, metric = catalog.find_metric(plan.metric)
    b = _Builder()

    # Components: a base metric is its own single component.
    if isinstance(metric, DerivedMetric):
        formula = ex.parse(metric.formula)
        component_refs = sorted(ex.names(formula))
    else:
        formula, component_refs = ex.Name(metric.name), [metric.name]

    ctes, refs, sources, group_names = [], {}, {dataset.slug: dataset}, []
    for i, ref in enumerate(component_refs):
        comp_dataset, comp_metric = catalog.find_metric(
            ref if "." in ref else f"{dataset.slug}.{ref}"
        )
        if isinstance(comp_metric, DerivedMetric):
            raise PlanError(f"Derived metric {metric.name!r} may only combine base metrics.")
        sql, group_names, touched = _component_sql(catalog, comp_dataset, comp_metric, plan, b)
        ctes.append(f"c{i} AS ({sql})")
        refs[ref] = f"c{i}"
        for slug in touched:  # the answer depends on every table joined, even just to filter
            sources.setdefault(slug, catalog.datasets[slug])

    cte_names = [f"c{i}" for i in range(len(ctes))]
    if group_names:
        using = ", ".join(q(g) for g in group_names)
        from_sql = cte_names[0] + "".join(f" FULL JOIN {c} USING ({using})" for c in cte_names[1:])
    else:
        # ON TRUE (not CROSS JOIN): one component having no data must not erase the others.
        from_sql = cte_names[0] + "".join(f" FULL JOIN {c} ON TRUE" for c in cte_names[1:])
    value_sql = _formula_sql(formula, refs)

    order = {
        "dimensions": ", ".join(q(g) for g in group_names),
        "metric_desc": f"{q(metric.name)} DESC NULLS LAST"
        + "".join(f", {q(g)}" for g in group_names),
        "metric_asc": f"{q(metric.name)} ASC NULLS LAST"
        + "".join(f", {q(g)}" for g in group_names),
    }[plan.order]
    sql = (  # same rule: catalog identifiers and bound parameters only
        f"WITH {', '.join(ctes)} "  # noqa: S608
        f"SELECT {', '.join([*(q(g) for g in group_names), f'{value_sql} AS {q(metric.name)}'])} "
        f"FROM {from_sql}"
        + (f" ORDER BY {order}" if order else "")
        + (f" LIMIT {b.bind(plan.limit)}" if plan.limit else "")
    )
    return CompiledQuery(
        sql=sql,
        params=b.params,
        columns=[*group_names, metric.name],
        metric=metric.name,
        unit=metric.unit,
        title=metric.title,
        sources=list(sources.values()),
        definition=" ".join(metric.description.split()),
    )
