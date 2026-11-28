"""Schema retrieval: choose which of the user's tables the planner is shown.

In "full" mode the planner reads a description of every table the user can read: about 11,000
tokens at 50 tables, on every question. In "retrieved" mode, this step picks a few likely
tables first and only those are described:

1. Each dataset version has one embedded description (a "card"): title, description, grain
   and its metrics. It's written when the version is prepared, in the same transaction.
2. The question is embedded, with dates removed first. Dates choose the time filter, never the
   table, and left in they matched any table whose description mentions months ("employed
   between January and April 2026"): in the M8b prototype, stripping them raised the share of
   questions with every needed table found from 44/47 to 47/47 at 50 tables.
3. The `k` closest cards, among the tables in the user's catalog only, are chosen.
4. Expansion adds what those tables need to work: tables they link to (so "by country" can
   join transactions to stores) and tables a derived metric's formula uses (net sales needs
   returns). Without it, the planner would see a metric it can't break down.

Permission comes first: the search runs only over the versions in the user's catalog, which is
built from what they can read now. A restricted table's card is never compared, ranked or
counted, so it can't influence which other tables are chosen either.

A table in the catalog without a card (published before cards existed, until `reindex` runs)
is always included rather than silently hidden.

The prototype also tried one entry per metric and keyword search fused with embeddings (as
document search does). Neither found more tables, so the simpler version was kept.
"""

import re
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.documents.embedder import Embedder, get_embedder
from app.ingest.expressions import names, parse
from app.ingest.manifest import Manifest
from app.models import SchemaEntry, SourceKind, SourceVersion
from app.query.catalog import Catalog

MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
DATE_WORDS = [
    re.compile(r"\b\d{4}-\d{2}(-\d{2})?\b"),  # 2026-03, 2026-03-31
    re.compile(r"\b(19|20)\d{2}\b"),  # 2026
    re.compile(r"\b\d{1,2}(st|nd|rd|th)?\b"),  # 31, 1st
    re.compile(rf"\b({MONTHS})\b", re.IGNORECASE),
    re.compile(r"\bq[1-4]\b", re.IGNORECASE),
]


def card_text(m: Manifest) -> str:
    """What a table is for, in the steward's words. Column lists are left out: in the
    prototype, adding them made matches worse (every table has a date and a store)."""
    text = f"{m.title}. {m.description.strip()} {m.grain.strip()}"
    metrics = [*m.metrics, *m.derived_metrics]
    if metrics:
        text += " Metrics: " + "; ".join(f"{x.title}: {x.description.strip()}" for x in metrics)
    return text


def search_text(question: str) -> str:
    """The question without dates: "revenue in March 2026" → "revenue in"."""
    for pattern in DATE_WORDS:
        question = pattern.sub(" ", question)
    return re.sub(r"\s+", " ", question).strip()


# ----------------------------------------------------------------------------- indexing


def index_schema(
    db: Session, version: SourceVersion, manifest: Manifest, embedder: Embedder | None = None
) -> None:
    """Write (or rewrite) the card for one dataset version. Idempotent; the caller commits."""
    embedder = embedder or get_embedder()
    text = card_text(manifest)
    db.execute(delete(SchemaEntry).where(SchemaEntry.source_version_id == version.id))
    db.add(
        SchemaEntry(
            source_version_id=version.id,
            text=text,
            embedding=embedder.passages([text])[0],
            embedding_model=embedder.model_name,
        )
    )
    db.flush()


def reindex_schema(db: Session, embedder: Embedder | None = None) -> int:
    """(Re)build the card of every stored dataset version: after a model change, or to backfill
    versions prepared before cards existed."""
    embedder = embedder or get_embedder()
    versions = db.scalars(
        select(SourceVersion)
        .join(SourceVersion.source)
        .where(SourceVersion.source.has(kind=SourceKind.DATASET))
    ).all()
    count = 0
    for v in versions:
        if "manifest" in (v.meta or {}):
            index_schema(db, v, Manifest.model_validate(v.meta["manifest"]), embedder)
            count += 1
    db.commit()
    return count


# ----------------------------------------------------------------------------- selection


@dataclass
class Selection:
    found: list[str]  # chosen by similarity, closest first
    added: list[str] = field(default_factory=list)  # needed by a chosen table (links, formulas)
    unindexed: list[str] = field(default_factory=list)  # no card yet: always included

    @property
    def tables(self) -> set[str]:
        return {*self.found, *self.added, *self.unindexed}

    def as_dict(self) -> dict:
        return {"found": self.found, "added": self.added, "unindexed": self.unindexed}


def needed_by(catalog: Catalog, slugs: list[str]) -> list[str]:
    """Readable tables that the given tables link to or whose metrics their formulas use,
    followed transitively, in a stable order."""
    seen, out, frontier = set(slugs), [], list(slugs)
    while frontier:
        m = catalog.datasets[frontier.pop(0)].manifest
        refs = [r.target_source for r in m.relationships]
        for d in m.derived_metrics:
            refs += [n.split(".")[0] for n in sorted(names(parse(d.formula))) if "." in n]
        for slug in refs:
            if slug in catalog.datasets and slug not in seen:
                seen.add(slug)
                out.append(slug)
                frontier.append(slug)
    return out


def select_tables(
    db: Session,
    catalog: Catalog,
    questions: list[str],
    k: int,
    embedder: Embedder | None = None,
) -> Selection:
    """The tables to describe for these questions (the new one, plus the previous turn's for a
    follow-up such as "and April?", which says nothing about tables on its own)."""
    embedder = embedder or get_embedder()
    by_version = {ds.version_id: ds.slug for ds in catalog.datasets.values()}
    indexed = {
        str(v)
        for v in db.scalars(
            select(SchemaEntry.source_version_id).where(
                SchemaEntry.source_version_id.in_(list(by_version))
            )
        )
    }
    found: list[str] = []
    for question in questions:
        vector = embedder.query(search_text(question) or question)
        distance = SchemaEntry.embedding.cosine_distance(vector)
        rows = db.execute(
            select(SchemaEntry.source_version_id)
            .where(SchemaEntry.source_version_id.in_(list(indexed)))
            .order_by(distance, SchemaEntry.source_version_id)
            .limit(k)
        ).scalars()
        found += [s for s in (by_version[str(v)] for v in rows) if s not in found]
    unindexed = sorted(slug for v, slug in by_version.items() if v not in indexed)
    added = [s for s in needed_by(catalog, found) if s not in unindexed]
    return Selection(found=found, added=added, unindexed=unindexed)
