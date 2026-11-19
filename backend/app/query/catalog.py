"""The catalog: what one user may query right now.

Built per request from the published versions of the datasets the user can read (via the
central authorization rules). Anything outside it, whether it doesn't exist or isn't the
user's to see, produces the same "unknown" error, so the catalog never leaks the existence
of restricted data.

It also resolves names in a query plan:
- metrics: "net_sales", or "returns.refund_value" when qualified;
- dimensions: "country" is found on the metric's own table or on a table reachable through
  declared relationships (transactions.store_id → stores.store_id), or "stores.country".
"""

from collections import deque
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import readable_sources_query
from app.ingest.manifest import Column, DerivedMetric, Manifest, Metric
from app.models import SourceKind, SourceVersion, User, VersionStatus

DIMENSION_ROLES = {"dimension", "id", "attribute"}


class PlanError(ValueError):
    """The plan can't be answered with this user's catalog. The message is safe to show."""


@dataclass(frozen=True)
class Dataset:
    slug: str
    version: int
    version_id: str
    table: str
    manifest: Manifest


@dataclass(frozen=True)
class Join:
    """LEFT JOIN `to_slug` ON from_slug.from_column = to_slug.to_column (many-to-one)."""

    from_slug: str
    from_column: str
    to_slug: str
    to_column: str


@dataclass(frozen=True)
class ResolvedColumn:
    dataset: Dataset
    column: Column
    path: tuple[Join, ...]  # joins from the metric's table to the column's table


class Catalog:
    def __init__(self, datasets: dict[str, Dataset]) -> None:
        self.datasets = datasets

    @classmethod
    def for_user(cls, db: Session, user: User) -> "Catalog":
        readable = readable_sources_query(user).subquery()
        rows = db.execute(
            select(readable.c.slug, SourceVersion)
            .join(SourceVersion, SourceVersion.source_id == readable.c.id)
            .where(
                readable.c.kind == SourceKind.DATASET,
                SourceVersion.status == VersionStatus.PUBLISHED,
            )
        ).all()
        return cls(
            {
                slug: Dataset(
                    slug=slug,
                    version=v.version,
                    version_id=str(v.id),
                    table=v.meta["table"],
                    manifest=Manifest.model_validate(v.meta["manifest"]),
                )
                for slug, v in rows
            }
        )

    # ------------------------------------------------------------------ metrics

    def find_metric(self, name: str) -> tuple[Dataset, Metric | DerivedMetric]:
        if "." in name:
            slug, metric_name = name.split(".", 1)
            candidates = [self.datasets[slug]] if slug in self.datasets else []
        else:
            metric_name, candidates = name, list(self.datasets.values())
        matches = [
            (ds, m)
            for ds in candidates
            for m in [*ds.manifest.metrics, *ds.manifest.derived_metrics]
            if m.name == metric_name
        ]
        if not matches:
            raise PlanError(f"Unknown metric {name!r}.")
        if len(matches) > 1:
            options = ", ".join(f"{ds.slug}.{m.name}" for ds, m in matches)
            raise PlanError(f"Metric {name!r} is ambiguous; use one of: {options}.")
        return matches[0]

    def metric_names(self) -> list[str]:
        return sorted(
            f"{ds.slug}.{m.name}"
            for ds in self.datasets.values()
            for m in [*ds.manifest.metrics, *ds.manifest.derived_metrics]
        )

    # ------------------------------------------------------------------ joins and dimensions

    def reachable(self, base: Dataset) -> dict[str, tuple[Join, ...]]:
        """Datasets reachable from `base` by following declared relationships (breadth-first),
        with the join path to each. Only many-to-one joins, so rows are never multiplied."""
        paths: dict[str, tuple[Join, ...]] = {base.slug: ()}
        queue = deque([base])
        while queue:
            current = queue.popleft()
            for rel in current.manifest.relationships:
                target = self.datasets.get(rel.target_source)
                if target is None or target.slug in paths:
                    continue  # unreadable or already reached
                join = Join(current.slug, rel.column, target.slug, rel.target_column)
                paths[target.slug] = (*paths[current.slug], join)
                queue.append(target)
        return paths

    def resolve_dimension(self, base: Dataset, name: str) -> ResolvedColumn:
        paths = self.reachable(base)
        if "." in name:
            slug, column_name = name.split(".", 1)
            scope = [slug] if slug in paths else []
        else:
            column_name = name
            # Prefer the metric's own table, then any reachable table.
            scope = [base.slug] if base.manifest.column(name) else list(paths)
        matches = []
        for slug in scope:
            col = self.datasets[slug].manifest.column(column_name)
            if col is not None and col.role in DIMENSION_ROLES:
                matches.append(ResolvedColumn(self.datasets[slug], col, paths[slug]))
        if not matches:
            raise PlanError(f"Unknown dimension {name!r} for {base.slug} data.")
        if len(matches) > 1:
            options = ", ".join(f"{m.dataset.slug}.{m.column.name}" for m in matches)
            raise PlanError(f"Dimension {name!r} is ambiguous; use one of: {options}.")
        return matches[0]
