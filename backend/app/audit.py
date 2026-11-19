"""Audit log: append a record of who did what. Called in the same transaction as the change,
so a change and its audit record are saved together or not at all."""

from sqlalchemy.orm import Session

from app.models import AuditLog, User


def record(db: Session, actor: User | None, action: str, target: str, **details: object) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor.id if actor else None,
            action=action,
            target=target,
            details={k: str(v) for k, v in details.items()},
        )
    )
