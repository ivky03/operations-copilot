"""Conversation history for follow-up questions ("And April?", "What was it before that?").

The planner sees a compact record of the last few turns: each question, how it was
understood, and what was looked up (metrics, filters, document searches). It never sees
data rows or previous answers' numbers, keeping the rule that rows never enter a prompt.
Clarification turns include our own question to the user, so a reply like "the blue one"
can be resolved.

Permissions apply to history too: a turn that used a source the user can no longer read is
left out of the context (and redacted when the conversation is viewed), so revoked access
can't come back through "what about that?".
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import readable_sources_query
from app.models import AnswerRun, User

MAX_TURNS = 3


@dataclass
class PriorTurn:
    question: str  # as the user typed it
    standalone: str  # as the planner understood it, complete on its own
    looked_up: list[str]  # what was queried or searched
    asked_user: str | None = None  # our clarification question, if that's how the turn ended


def readable_slugs(db: Session, user: User) -> set[str]:
    if not user.is_active:
        return set()
    return {s.slug for s in db.scalars(readable_sources_query(user))}


def visible(run: AnswerRun, readable: set[str]) -> bool:
    """A turn is visible only if the user can still read every source it used, and its content
    wasn't erased because one of them was deleted."""
    if run.redacted_reason:
        return False
    return all(s["source"] in readable for s in run.source_versions)


def _looked_up(run: AnswerRun) -> list[str]:
    items = [e["description"] for e in run.evidence if e.get("description")]
    searches = run.plan.get("doc_searches") or []
    if searches:
        history = " (including superseded versions)" if run.plan.get("include_history") else ""
        items.append(f"documents searched for: {', '.join(searches)}{history}")
    return items


def prior_turns(db: Session, user: User, conversation_id: uuid.UUID) -> list[PriorTurn]:
    readable = readable_slugs(db, user)
    runs = db.scalars(
        select(AnswerRun)
        .where(AnswerRun.conversation_id == conversation_id, AnswerRun.status != "error")
        .order_by(AnswerRun.created_at.desc())
        .limit(MAX_TURNS * 3)  # some may be hidden; look a little further back
    ).all()
    turns = [
        PriorTurn(
            question=run.question,
            standalone=run.plan.get("standalone_question") or run.question,
            looked_up=_looked_up(run),
            asked_user=run.answer if run.status == "clarification" else None,
        )
        for run in runs
        if visible(run, readable)
    ][:MAX_TURNS]
    return list(reversed(turns))


def render(turns: list[PriorTurn]) -> str:
    lines = []
    for n, t in enumerate(turns, start=1):
        lines.append(f'Turn {n}: "{t.question}"')
        if t.standalone != t.question:
            lines.append(f'  understood as: "{t.standalone}"')
        for item in t.looked_up:
            lines.append(f"  looked up: {item}")
        if t.asked_user:
            lines.append(f'  we asked the user: "{t.asked_user}"')
    return "\n".join(lines)
