"""Document evidence: the passages an answer may quote, each with an ID (C1, C2, ...).

The planner proposes up to three short searches ("return window", "refund timing"). Each runs
through the permission-filtered hybrid search from M4a, so a passage the user can't read is
never retrieved, never sent to the model and never shown. Results are interleaved (best of
each search first) and de-duplicated, so a combined question gets passages for every part.
"""

from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import readable_sources_query
from app.documents.search import search
from app.models import Source, SourceKind, SourceVersion, User, VersionStatus

PER_SEARCH = 4
MAX_PASSAGES = 6


@dataclass
class Passage:
    id: str  # C1, C2, ...
    source: str  # document slug
    version: int
    doc_title: str
    heading: str
    effective_date: str | None
    current: bool  # False for a superseded version (only retrieved when history is asked for)
    content: str

    @property
    def label(self) -> str:
        return f"{self.source}@v{self.version}"

    def as_dict(self) -> dict:
        return asdict(self)


def retrieve_passages(
    db: Session, user: User, searches: list[str], include_history: bool = False
) -> list[Passage]:
    ranked = [
        search(db, user, text, k=PER_SEARCH, include_superseded=include_history)
        for text in searches
        if text.strip()
    ]
    passages: list[Passage] = []
    seen = set()
    for position in range(PER_SEARCH):  # round-robin: every search's best hit comes first
        for hits in ranked:
            if position >= len(hits) or hits[position].chunk_id in seen:
                continue
            hit = hits[position]
            seen.add(hit.chunk_id)
            passages.append(
                Passage(
                    id=f"C{len(passages) + 1}",
                    source=hit.source,
                    version=hit.version,
                    doc_title=hit.doc_title,
                    heading=hit.heading,
                    effective_date=hit.effective_date,
                    current=hit.current,
                    content=hit.content,
                )
            )
            if len(passages) == MAX_PASSAGES:
                return passages
    return passages


def document_titles(db: Session, user: User) -> list[str]:
    """Titles of the published documents this user can read, for the planner's prompt.
    Restricted documents are left out, so the prompt doesn't reveal that they exist."""
    if not user.is_active:
        return []
    readable = readable_sources_query(user).where(Source.kind == SourceKind.DOCUMENT).subquery()
    rows = db.scalars(
        select(SourceVersion.meta["title"].astext)
        .join(readable, readable.c.id == SourceVersion.source_id)
        .where(SourceVersion.status == VersionStatus.PUBLISHED)
    ).all()
    return sorted(t for t in rows if t)
