"""Administrator endpoints: users, group membership, source registration and grants.

Every route depends on `require_admin`, so non-admins get 403 and anonymous callers get 401.
Every change is written to the audit log in the same transaction.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.auth import UserOut
from app.authz import require_admin
from app.db import get_db
from app.ingest.manifest import DATASET_SLUG, SLUG
from app.models import (
    Group,
    Role,
    Source,
    SourceGrant,
    SourceKind,
    SourceVersion,
    User,
    VersionStatus,
)
from app.security import MIN_PASSWORD_LENGTH, hash_password

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class CreateUser(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)
    role: Role = Role.EMPLOYEE
    groups: list[str] = []


class SetActive(BaseModel):
    is_active: bool


class SetGrants(BaseModel):
    groups: list[str]


class RegisterSource(BaseModel):
    slug: str = Field(pattern=f"{SLUG}|{DATASET_SLUG}", max_length=100)
    kind: SourceKind
    title: str = Field(min_length=1, max_length=300)
    groups: list[str] = Field(min_length=1)  # who may read it; a source nobody can read is useless


class GroupOut(BaseModel):
    slug: str
    name: str


class AdminSourceOut(BaseModel):
    """Admins see every source and who can read it, but not its content (no data access)."""

    slug: str
    kind: str
    title: str
    status: str
    groups: list[str]
    published_version: int | None


def _groups_by_slug(db: Session, slugs: list[str]) -> list[Group]:
    found = list(db.scalars(select(Group).where(Group.slug.in_(slugs))))
    missing = set(slugs) - {g.slug for g in found}
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown groups: {sorted(missing)}"
        )
    return found


@router.get("/users")
def list_users(db: Session = Depends(get_db)) -> list[UserOut]:
    return [UserOut.of(u) for u in db.scalars(select(User).order_by(User.email))]


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    body: CreateUser, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> UserOut:
    email = body.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with this email already exists.")
    user = User(
        email=email,
        display_name=body.display_name,
        password_hash=hash_password(body.password),
        role=body.role,
        groups=_groups_by_slug(db, body.groups),
    )
    db.add(user)
    audit.record(db, admin, "user.create", email, role=body.role.value, groups=body.groups)
    db.commit()
    return UserOut.of(user)


@router.patch("/users/{email}")
def set_user_active(
    email: str, body: SetActive, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> UserOut:
    """Deactivate (or reactivate) a user. Takes effect on their very next request."""
    user = db.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    if user.id == admin.id and not body.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, "You can't deactivate yourself.")
    user.is_active = body.is_active
    audit.record(db, admin, "user.set_active", email, is_active=body.is_active)
    db.commit()
    return UserOut.of(user)


@router.put("/users/{email}/groups")
def set_user_groups(
    email: str, body: SetGrants, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> UserOut:
    user = db.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    before = sorted(g.slug for g in user.groups)
    user.groups = _groups_by_slug(db, body.groups)
    audit.record(db, admin, "user.set_groups", email, before=before, after=sorted(body.groups))
    db.commit()
    return UserOut.of(user)


@router.put("/sources/{slug}/grants")
def set_source_grants(
    slug: str, body: SetGrants, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> dict:
    """Replace which groups can read a source. Revocation applies to the next request."""
    source = db.scalar(select(Source).where(Source.slug == slug))
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found.")
    before = sorted(g.slug for g in source.granted_groups)
    groups = _groups_by_slug(db, body.groups)
    db.query(SourceGrant).filter(SourceGrant.source_id == source.id).delete()
    db.add_all(SourceGrant(source_id=source.id, group_id=g.id) for g in groups)
    audit.record(db, admin, "source.set_grants", slug, before=before, after=sorted(body.groups))
    db.commit()
    return {"source": slug, "groups": sorted(body.groups)}


@router.get("/groups")
def list_groups(db: Session = Depends(get_db)) -> list[GroupOut]:
    return [
        GroupOut(slug=g.slug, name=g.name) for g in db.scalars(select(Group).order_by(Group.slug))
    ]


def _admin_source(db: Session, source: Source) -> AdminSourceOut:
    published = db.scalar(
        select(SourceVersion.version).where(
            SourceVersion.source_id == source.id, SourceVersion.status == VersionStatus.PUBLISHED
        )
    )
    return AdminSourceOut(
        slug=source.slug,
        kind=source.kind.value,
        title=source.title,
        status=source.status.value,
        groups=sorted(g.slug for g in source.granted_groups),
        published_version=published,
    )


@router.get("/sources")
def list_all_sources(db: Session = Depends(get_db)) -> list[AdminSourceOut]:
    return [_admin_source(db, s) for s in db.scalars(select(Source).order_by(Source.slug))]


@router.post("/sources", status_code=status.HTTP_201_CREATED)
def register_source(
    body: RegisterSource, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> AdminSourceOut:
    """Register a new dataset or document and decide who may read it. It has no content until
    a steward in one of those groups uploads a first version, which is then reviewed and
    published like any other."""
    if body.kind == SourceKind.DATASET and not re.match(DATASET_SLUG, body.slug):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Dataset names may use lowercase letters, digits and underscores "
            "(e.g. purchase_orders): queries refer to them as dataset.column.",
        )
    if db.scalar(select(Source).where(Source.slug == body.slug)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"A source called {body.slug!r} exists.")
    groups = _groups_by_slug(db, body.groups)
    source = Source(slug=body.slug, kind=body.kind, title=body.title, granted_groups=groups)
    db.add(source)
    audit.record(
        db, admin, "source.register", body.slug, kind=body.kind.value, grants=sorted(body.groups)
    )
    db.commit()
    return _admin_source(db, source)
