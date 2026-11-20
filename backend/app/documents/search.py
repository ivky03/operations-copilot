"""Hybrid document search, filtered by permission BEFORE ranking.

Two retrievers, each good at what the other misses:
- Keyword (Postgres full-text): exact terms such as "SPRING10", "C prefix", "€75".
- Meaning (vector similarity): paraphrases such as "how long can customers send things back"
  for a section titled "Return window".

Their rankings are merged with Reciprocal Rank Fusion (RRF): each passage scores
sum(1 / (60 + rank)) over the lists it appears in. RRF uses only positions, so the two very
different score scales (text rank vs cosine similarity) never need calibrating.

Permissions are part of each SQL query's WHERE clause, so a restricted passage is never
fetched, ranked or counted. It can't leak through a score, a "did you mean" or a log line.
By default only the current (published) version of each document is searched; superseded
versions are included only when asked for, e.g. "what was the policy before March?".
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.authz import readable_sources_query
from app.documents.embedder import Embedder, get_embedder
from app.models import DocChunk, Source, SourceVersion, User, VersionStatus

RRF_K = 60
CANDIDATES = 20  # taken from each retriever before fusing
Mode = Literal["hybrid", "keyword", "vector"]
WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Hit:
    chunk_id: uuid.UUID
    source: str  # document slug
    doc_title: str
    version: int
    current: bool  # False for a superseded version
    effective_date: str | None
    heading: str
    content: str
    score: float
    ranks: dict[str, int] = field(default_factory=dict)  # position in each retriever's list

    @property
    def label(self) -> str:
        return f"{self.source}@v{self.version}"


def _filtered(user: User, include_superseded: bool):
    """chunks JOIN versions, restricted to readable sources and allowed version states."""
    statuses = [VersionStatus.PUBLISHED]
    if include_superseded:
        statuses.append(VersionStatus.SUPERSEDED)
    readable = readable_sources_query(user).with_only_columns(Source.id).subquery()
    return (
        select(DocChunk.id)
        .join(SourceVersion, SourceVersion.id == DocChunk.source_version_id)
        .where(SourceVersion.source_id.in_(select(readable.c.id)))
        .where(SourceVersion.status.in_(statuses))
    )


def _keyword_query(text: str) -> str | None:
    """OR together the question's words. AND (websearch syntax) would demand every word,
    including 'our' and 'policy', and miss most passages. Words are reduced to [a-z0-9], so
    nothing the user types can change the query's syntax."""
    words = list(dict.fromkeys(WORD.findall(text.lower())))
    return " | ".join(words) if words else None


def _keyword_ranking(db: Session, base, text: str) -> list[uuid.UUID]:
    q = _keyword_query(text)
    if q is None:
        return []
    tsq = func.to_tsquery("english", q)
    stmt = (
        base.where(DocChunk.search_text.op("@@")(tsq))
        .order_by(func.ts_rank_cd(DocChunk.search_text, tsq).desc(), DocChunk.id)
        .limit(CANDIDATES)
    )
    return list(db.scalars(stmt))


def _vector_ranking(db: Session, base, vector: list[float]) -> list[uuid.UUID]:
    stmt = base.order_by(DocChunk.embedding.cosine_distance(vector), DocChunk.id).limit(CANDIDATES)
    return list(db.scalars(stmt))


def search(
    db: Session,
    user: User,
    text: str,
    k: int = 5,
    include_superseded: bool = False,
    mode: Mode = "hybrid",
    embedder: Embedder | None = None,
) -> list[Hit]:
    if not user.is_active or not text.strip():
        return []
    base = _filtered(user, include_superseded)
    rankings: dict[str, list[uuid.UUID]] = {}
    if mode in ("hybrid", "keyword"):
        rankings["keyword"] = _keyword_ranking(db, base, text)
    if mode in ("hybrid", "vector"):
        rankings["vector"] = _vector_ranking(db, base, (embedder or get_embedder()).query(text))

    scores: dict[uuid.UUID, float] = {}
    ranks: dict[uuid.UUID, dict[str, int]] = {}
    for name, ids in rankings.items():
        for position, chunk_id in enumerate(ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + position)
            ranks.setdefault(chunk_id, {})[name] = position
    top = sorted(scores, key=lambda c: (-scores[c], str(c)))[:k]
    if not top:
        return []

    rows = db.execute(
        select(
            DocChunk,
            Source.slug,
            SourceVersion.version,
            SourceVersion.status,
            SourceVersion.meta["effective_date"].astext.label("effective_date"),
        )
        .join(SourceVersion, SourceVersion.id == DocChunk.source_version_id)
        .join(Source, Source.id == SourceVersion.source_id)
        .where(DocChunk.id.in_(top))
    ).all()
    by_id = {row[0].id: row for row in rows}
    hits = []
    for chunk_id in top:
        chunk, slug, version, status, effective = by_id[chunk_id]
        hits.append(
            Hit(
                chunk_id=chunk.id,
                source=slug,
                doc_title=chunk.doc_title,
                version=version,
                current=status == VersionStatus.PUBLISHED,
                effective_date=str(effective) if isinstance(effective, str | date) else None,
                heading=chunk.heading,
                content=chunk.content,
                score=round(scores[chunk_id], 6),
                ranks=ranks[chunk_id],
            )
        )
    return hits
