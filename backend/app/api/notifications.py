"""In-app notifications, and following a source to receive them.

GET  /api/notifications            your latest notifications and the unread count
POST /api/notifications/read       mark some (ids) or all of them read
PUT/DELETE /api/sources/{slug}/subscription   follow / unfollow a source you can read

Only notifications about sources you can read now are listed or counted (app/notifications.py).
Following a source you can't read gets 404, as if it didn't exist.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import notifications
from app.auth import current_user
from app.authz import get_readable_source
from app.db import get_db
from app.models import Notification, Source, Subscription, User

router = APIRouter(prefix="/api", tags=["notifications"])


class NotificationOut(BaseModel):
    id: str
    source: str
    kind: str
    version: int | None
    message: str
    created_at: datetime
    read: bool


class NotificationsOut(BaseModel):
    unread: int
    items: list[NotificationOut]


class MarkRead(BaseModel):
    ids: list[uuid.UUID] | None = None  # None: all of them


class SubscriptionOut(BaseModel):
    source: str
    subscribed: bool


def _list(db: Session, user: User) -> NotificationsOut:
    items = [
        NotificationOut(
            id=str(n.id),
            source=slug,
            kind=n.kind,
            version=n.version,
            message=notifications.message(n.kind, title, n.version),
            created_at=n.created_at,
            read=n.read_at is not None,
        )
        for n, slug, title in notifications.visible_notifications(db, user)
    ]
    return NotificationsOut(unread=notifications.unread_count(db, user), items=items)


@router.get("/notifications")
def list_notifications(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> NotificationsOut:
    return _list(db, user)


@router.post("/notifications/read")
def mark_read(
    body: MarkRead, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> NotificationsOut:
    query = update(Notification).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)
    )
    if body.ids is not None:
        query = query.where(Notification.id.in_(body.ids))
    db.execute(query.values(read_at=datetime.now(UTC)))
    db.commit()
    return _list(db, user)


@router.put("/sources/{slug}/subscription")
def follow(
    source: Source = Depends(get_readable_source),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SubscriptionOut:
    db.execute(
        insert(Subscription)
        .values(user_id=user.id, source_id=source.id)
        .on_conflict_do_nothing()  # following twice is still following
    )
    db.commit()
    return SubscriptionOut(source=source.slug, subscribed=True)


@router.delete("/sources/{slug}/subscription", status_code=status.HTTP_204_NO_CONTENT)
def unfollow(
    source: Source = Depends(get_readable_source),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    db.execute(
        delete(Subscription).where(
            Subscription.user_id == user.id, Subscription.source_id == source.id
        )
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
