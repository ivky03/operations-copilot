"""Endpoints any logged-in user can call, scoped to the sources they're allowed to read."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import current_user
from app.authz import get_readable_source, readable_sources_query
from app.db import get_db
from app.models import Source, User

router = APIRouter(prefix="/api/sources", tags=["sources"])


class SourceOut(BaseModel):
    slug: str
    kind: str
    title: str

    @classmethod
    def of(cls, s: Source) -> "SourceOut":
        return cls(slug=s.slug, kind=s.kind.value, title=s.title)


@router.get("")
def list_sources(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[SourceOut]:
    """Only sources the user can read. Restricted sources are simply absent, not marked."""
    rows = db.scalars(readable_sources_query(user).order_by(Source.slug))
    return [SourceOut.of(s) for s in rows]


@router.get("/{slug}")
def get_source(source: Source = Depends(get_readable_source)) -> SourceOut:
    return SourceOut.of(source)
