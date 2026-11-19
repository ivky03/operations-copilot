"""Schema context: the description of available data that the planner model reads.

Built per user from their catalog, so restricted datasets never appear in a prompt.
Contains metrics, groupable columns with a few example values, relationships and the date
range covered. Never table rows.

This is "full" mode: every readable dataset is described. With many tables (Stage C) a
retrieval step will choose which datasets to include; the M8 experiment compares the two.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ingest.publish import DATASET_SCHEMA
from app.query.catalog import Catalog, Dataset
from app.query.compiler import q
from app.query.executor import run_readonly

MAX_EXAMPLES = 12


@dataclass
class SchemaContext:
    text: str
    data_start: date | None
    data_end: date | None


def _coverage(ds: Dataset) -> tuple[date | None, date | None]:
    col = ds.manifest.time_column
    if not col:
        return None, None
    sql = f"SELECT min({q(col)})::date, max({q(col)})::date FROM {DATASET_SCHEMA}.{q(ds.table)}"  # noqa: S608
    row = run_readonly(sql, {}, ["start", "end"]).rows[0]
    return row["start"], row["end"]


def _examples(db: Session, ds: Dataset) -> dict[str, list[str]]:
    rows = db.execute(
        text(
            "SELECT column_name, array_agg(canonical ORDER BY canonical) FROM value_index "
            "WHERE source_version_id = CAST(:v AS uuid) AND NOT is_alias GROUP BY column_name"
        ),
        {"v": ds.version_id},
    ).all()
    return {col: list(values) for col, values in rows}


def build_context(db: Session, catalog: Catalog) -> SchemaContext:
    lines: list[str] = []
    starts, ends = [], []
    for ds in sorted(catalog.datasets.values(), key=lambda d: d.slug):
        m = ds.manifest
        start, end = _coverage(ds)
        if start:
            starts.append(start)
            ends.append(end)
        lines.append(f"## Dataset `{ds.slug}`: {m.title}")
        lines.append(m.description.strip())
        lines.append(f"Grain: {m.grain.strip()}")
        if m.time_column:
            lines.append(f"Date column: {m.time_column}; data from {start} to {end}.")
        for rel in m.relationships:
            lines.append(f"Linked: {m.source}.{rel.column} → {rel.references}")
        all_metrics = [*m.metrics, *m.derived_metrics]
        if all_metrics:
            lines.append("Metrics:")
            for metric in all_metrics:
                unit = f" ({metric.unit})" if metric.unit else ""
                lines.append(
                    f"- `{metric.name}`{unit}: {metric.title}. {metric.description.strip()}"
                )
        examples = _examples(db, ds)
        dims = [
            c for c in m.columns if c.role in {"dimension", "attribute"} and c.type != "decimal"
        ]
        if dims:
            lines.append("Columns you can group or filter by:")
            for col in dims:
                values = examples.get(col.name, [])
                shown = ", ".join(values[:MAX_EXAMPLES]) + (
                    ", …" if len(values) > MAX_EXAMPLES else ""
                )
                example = f" Values: {shown}." if shown else ""
                lines.append(f"- `{col.name}` ({col.type}): {col.description.strip()}{example}")
        lines.append("")
    return SchemaContext(
        text="\n".join(lines).strip(),
        data_start=min(starts) if starts else None,
        data_end=max(ends) if ends else None,
    )
