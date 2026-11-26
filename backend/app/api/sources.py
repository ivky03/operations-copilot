"""Endpoints any logged-in user can call, scoped to the sources they're allowed to read."""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user
from app.authz import get_readable_source, readable_sources_query
from app.db import get_db
from app.models import Source, SourceVersion, User, VersionStatus
from app.notifications import subscribed_ids

router = APIRouter(prefix="/api/sources", tags=["sources"])


class SourceOut(BaseModel):
    slug: str
    kind: str
    title: str
    published_version: int | None  # None until a version has been published
    published_at: datetime | None
    subscribed: bool = False  # you follow it: you're notified when a new version is published

    @classmethod
    def of(cls, s: Source, v: SourceVersion | None, subscribed: bool = False) -> "SourceOut":
        return cls(
            slug=s.slug,
            kind=s.kind.value,
            title=s.title,
            published_version=v.version if v else None,
            published_at=v.published_at if v else None,
            subscribed=subscribed,
        )


def _published(db: Session, source_ids: list) -> dict:
    rows = db.scalars(
        select(SourceVersion).where(
            SourceVersion.source_id.in_(source_ids),
            SourceVersion.status == VersionStatus.PUBLISHED,
        )
    )
    return {v.source_id: v for v in rows}


@router.get("")
def list_sources(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[SourceOut]:
    """Only sources the user can read. Restricted sources are simply absent, not marked."""
    sources = list(db.scalars(readable_sources_query(user).order_by(Source.slug)))
    ids = [s.id for s in sources]
    published, followed = _published(db, ids), subscribed_ids(db, user, ids)
    return [SourceOut.of(s, published.get(s.id), s.id in followed) for s in sources]


@router.get("/{slug}")
def get_source(
    source: Source = Depends(get_readable_source),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SourceOut:
    followed = source.id in subscribed_ids(db, user, [source.id])
    return SourceOut.of(source, _published(db, [source.id]).get(source.id), followed)
