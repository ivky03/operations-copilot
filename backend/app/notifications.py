"""Subscriptions and in-app notifications.

A user follows a source they can read. When a new version of it is published, each follower who
can read it *at that moment* gets a notification. The publisher doesn't notify themselves.

Permissions apply when a notification is read, too: the list only shows notifications about
sources the reader can read now. Revoked access or an archived source hides them, and they come
back if access does. Deleting a source deletes its subscriptions and notifications.
A notification stores no text, only (source, kind, version); the message is built when it's
read, from the source's current title.
"""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.authz import readable_sources_query
from app.models import (
    Notification,
    Source,
    SourceGrant,
    SourceStatus,
    SourceVersion,
    Subscription,
    User,
    UserGroup,
)

PUBLISHED = "published"
LIST_LIMIT = 30


def notify_published(
    db: Session, source: Source, version: SourceVersion, actor: User | None
) -> int:
    """Tell followers who can read the source that a new version is live. Doesn't commit: it's
    part of the publish transaction, so a publish that rolls back notifies no one."""
    if source.status != SourceStatus.ACTIVE:
        return 0
    can_read = (
        select(UserGroup.user_id)
        .join(SourceGrant, SourceGrant.group_id == UserGroup.group_id)
        .where(SourceGrant.source_id == source.id)
    )
    followers = db.scalars(
        select(Subscription.user_id)
        .join(User, User.id == Subscription.user_id)
        .where(
            Subscription.source_id == source.id,
            Subscription.user_id.in_(can_read),
            User.is_active.is_(True),
        )
    ).all()
    recipients = [u for u in followers if actor is None or u != actor.id]
    db.add_all(
        Notification(user_id=u, source_id=source.id, kind=PUBLISHED, version=version.version)
        for u in recipients
    )
    return len(recipients)


def forget_source(db: Session, source_id: uuid.UUID) -> None:
    """A deleted source: nobody follows it and nothing is said about it any more."""
    db.execute(delete(Notification).where(Notification.source_id == source_id))
    db.execute(delete(Subscription).where(Subscription.source_id == source_id))


def subscribed_ids(db: Session, user: User, source_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    return set(
        db.scalars(
            select(Subscription.source_id).where(
                Subscription.user_id == user.id, Subscription.source_id.in_(source_ids)
            )
        )
    )


def _visible(user: User):
    """The user's notifications about sources they can read now."""
    readable = readable_sources_query(user).subquery()
    return (
        select(Notification, readable.c.slug, readable.c.title)
        .join(readable, readable.c.id == Notification.source_id)
        .where(Notification.user_id == user.id)
    )


def visible_notifications(db: Session, user: User) -> list[tuple[Notification, str, str]]:
    if not user.is_active:
        return []
    rows = db.execute(_visible(user).order_by(Notification.created_at.desc()).limit(LIST_LIMIT))
    return [(n, slug, title) for n, slug, title in rows]


def unread_count(db: Session, user: User) -> int:
    if not user.is_active:
        return 0
    query = _visible(user).where(Notification.read_at.is_(None)).subquery()
    return db.scalar(select(func.count()).select_from(query)) or 0


def message(kind: str, title: str, version: int | None) -> str:
    if kind == PUBLISHED:
        return f"{title}: version {version} was published. New answers use it."
    return title
