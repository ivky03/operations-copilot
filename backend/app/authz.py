"""Authorization: the single place that decides what a user may see and do.

Two rules, enforced on the server for every request:
1. Roles gate *actions*: only admins manage users and grants.
2. Group grants gate *data*: a user can read a source only if one of their groups is granted
   it, the source is active, and the user is active. Admins get no implicit data access.
   Granting themselves access is an audited action like any other.

Permissions are computed fresh on every request, never cached in the login session, so a
revoked grant or a deactivated user takes effect immediately.
"""

import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user
from app.db import get_db
from app.models import Role, Source, SourceGrant, SourceStatus, User, UserGroup


def require_role(*allowed: Role):
    """FastAPI dependency factory: 403 unless the current user has one of the allowed roles."""

    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You don't have permission to do this.")
        return user

    return dependency


require_admin = require_role(Role.ADMIN)


def readable_sources_query(user: User):
    """SELECT of sources the user may read. Composable: other queries can join or filter on it,
    so every feature (search, SQL, downloads) applies the same rule at the database level."""
    return (
        select(Source)
        .join(SourceGrant, SourceGrant.source_id == Source.id)
        .join(UserGroup, UserGroup.group_id == SourceGrant.group_id)
        .where(UserGroup.user_id == user.id, Source.status == SourceStatus.ACTIVE)
        .distinct()
    )


def readable_source_ids(db: Session, user: User) -> set[uuid.UUID]:
    if not user.is_active:
        return set()
    return {s.id for s in db.scalars(readable_sources_query(user))}


def can_read_source(db: Session, user: User, source_id: uuid.UUID) -> bool:
    return source_id in readable_source_ids(db, user)


def get_readable_source(
    slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Source:
    """Dependency for routes about one source. Unreadable and nonexistent sources both return 404,
    so the response doesn't confirm that a restricted source exists."""
    source = db.scalar(readable_sources_query(user).where(Source.slug == slug))
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found.")
    return source
