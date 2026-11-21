"""Asking questions and keeping conversations.

POST /api/ask                      ask; starts a new conversation unless conversation_id is given
POST /api/ask/stream               the same, as server-sent events: progress, data and passages
                                   as soon as they exist, then the verified answer
GET  /api/conversations            your conversations, most recent first
GET  /api/conversations/{id}       one conversation with every turn's answer and evidence
PATCH/DELETE /api/conversations/{id}

Conversations are private to their owner: anyone else gets 404, as if it didn't exist.
A saved turn that used a source the viewer can no longer read is redacted when shown.
The language-model clients are FastAPI dependencies so tests can replace them with a fake.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.answer.history import readable_slugs, visible
from app.answer.pipeline import AnswerResult, answer_question
from app.auth import current_user
from app.config import get_settings
from app.db import _session_factory, get_db
from app.llm.client import LLMClient, build_client
from app.models import AnswerRun, Conversation, User

router = APIRouter(prefix="/api", tags=["ask"])
log = logging.getLogger(__name__)

REDACTED = "This answer used a source you no longer have access to, so it's hidden."
TITLE_LENGTH = 80


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    conversation_id: uuid.UUID | None = None


class EvidenceOut(BaseModel):
    id: str
    title: str
    metric: str
    unit: str | None
    description: str
    columns: list[str]
    rows: list[dict[str, str | float | int | None]]
    comparisons: list[dict[str, str | float | int | None]]
    sql: str
    sources: list[dict[str, str | int]]


class PassageOut(BaseModel):
    id: str
    source: str
    version: int
    doc_title: str
    heading: str
    effective_date: str | None
    current: bool
    content: str


class AskResponse(BaseModel):
    """One turn: the question and its answer. Also the shape of each turn in a conversation."""

    run_id: str
    conversation_id: str | None = None
    question: str
    standalone_question: str | None = None  # how a follow-up was understood ("And April?" → …)
    status: str
    answer: str
    options: list[str]
    notes: list[str]
    evidence: list[EvidenceOut]
    passages: list[PassageOut]
    sources: list[dict[str, str | int]]
    latency_ms: int


class ConversationSummary(BaseModel):
    id: str
    title: str
    updated_at: datetime


class ConversationOut(ConversationSummary):
    turns: list[AskResponse]


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


def get_planner_llm() -> LLMClient:
    return build_client(purpose_model=get_settings().llm_planner_model)


def get_writer_llm() -> LLMClient:
    return build_client(purpose_model=get_settings().llm_writer_model)


def _own_conversation(db: Session, user: User, conversation_id: uuid.UUID) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    return conversation


def _title(question: str) -> str:
    q = " ".join(question.split())
    return q if len(q) <= TITLE_LENGTH else q[: TITLE_LENGTH - 1].rstrip() + "…"


def _from_result(result: AnswerResult, conversation: Conversation) -> AskResponse:
    return AskResponse(
        run_id=result.run_id,
        conversation_id=str(conversation.id),
        question=result.question,
        standalone_question=result.standalone_question,
        status=result.status,
        answer=result.answer,
        options=result.options,
        notes=result.notes,
        evidence=[EvidenceOut(**e.as_dict()) for e in result.evidence],
        passages=[PassageOut(**p.as_dict()) for p in result.passages],
        sources=result.sources,
        latency_ms=result.latency_ms,
    )


def _from_run(run: AnswerRun, readable: set[str]) -> AskResponse:
    base = {
        "run_id": str(run.id),
        "conversation_id": str(run.conversation_id),
        "question": run.question,
        "latency_ms": run.latency_ms or 0,
    }
    if not visible(run, readable):
        return AskResponse(
            **base, status="redacted", answer=REDACTED, options=[], notes=[], evidence=[],
            passages=[], sources=[],
        )  # fmt: skip
    restated = run.plan.get("standalone_question") if run.conversation_id else None
    return AskResponse(
        **base,
        standalone_question=restated if restated and restated != run.question else None,
        status=run.status,
        answer=run.answer or "",
        options=run.verification.get("options", []),
        notes=run.verification.get("notes", []),
        evidence=[EvidenceOut(**e) for e in run.evidence],
        passages=[PassageOut(**p) for p in run.passages],
        sources=run.source_versions,
    )


def _conversation_for(db: Session, user: User, body: AskRequest) -> Conversation:
    if body.conversation_id:
        return _own_conversation(db, user, body.conversation_id)
    conversation = Conversation(user_id=user.id, title=_title(body.question))
    db.add(conversation)
    db.flush()
    return conversation


def _public(event: str, data: dict) -> dict:
    """Event payloads use the same shapes as the API's responses (no internal fields)."""
    if event == "evidence":
        return {**data, "items": [EvidenceOut(**i).model_dump() for i in data["items"]]}
    if event == "passages":
        return {"items": [PassageOut(**i).model_dump() for i in data["items"]]}
    return data


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


@router.post("/ask/stream")
async def ask_stream(
    body: AskRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    planner: LLMClient = Depends(get_planner_llm),
    writer: LLMClient = Depends(get_writer_llm),
) -> StreamingResponse:
    """Server-sent events: `step`, `planned`, `evidence`, `passages`, then `answer` (the same
    object /api/ask returns) or `error`. The answer text arrives only after verification."""
    conversation = _conversation_for(db, user, body)
    db.commit()  # the worker uses its own session, so the conversation must be committed
    user_id, conversation_id = user.id, conversation.id
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

    def put(item: tuple[str, dict] | None) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def work() -> None:
        # Its own database session: the request's session belongs to the request's lifetime,
        # while this thread keeps working until the answer is saved (even if the client left).
        with _session_factory()() as own:
            try:
                me, convo = own.get(User, user_id), own.get(Conversation, conversation_id)
                result = answer_question(
                    own, me, body.question, planner, writer, convo,
                    on_event=lambda event, data: put((event, _public(event, data))),
                )  # fmt: skip
                put(("answer", _from_result(result, convo).model_dump(mode="json")))
            except Exception:
                log.exception("streamed answer failed")
                put(("error", {"message": "Something went wrong. Please try again."}))
            finally:
                put(None)

    async def events():
        loop.run_in_executor(None, work)
        while (item := await queue.get()) is not None:
            yield _sse(*item)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/ask")
async def ask(
    body: AskRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    planner: LLMClient = Depends(get_planner_llm),
    writer: LLMClient = Depends(get_writer_llm),
) -> AskResponse:
    conversation = _conversation_for(db, user, body)
    # The pipeline makes blocking network and database calls: keep them off the event loop.
    result = await run_in_threadpool(
        answer_question, db, user, body.question, planner, writer, conversation
    )
    return _from_result(result, conversation)


@router.get("/conversations")
def list_conversations(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[ConversationSummary]:
    rows = db.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
    )
    return [ConversationSummary(id=str(c.id), title=c.title, updated_at=c.updated_at) for c in rows]


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> ConversationOut:
    conversation = _own_conversation(db, user, conversation_id)
    readable = readable_slugs(db, user)
    runs = db.scalars(
        select(AnswerRun)
        .where(AnswerRun.conversation_id == conversation.id)
        .order_by(AnswerRun.created_at)
    )
    return ConversationOut(
        id=str(conversation.id),
        title=conversation.title,
        updated_at=conversation.updated_at,
        turns=[_from_run(r, readable) for r in runs],
    )


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: uuid.UUID,
    body: RenameRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ConversationSummary:
    conversation = _own_conversation(db, user, conversation_id)
    conversation.title = body.title.strip() or conversation.title
    db.commit()
    return ConversationSummary(
        id=str(conversation.id), title=conversation.title, updated_at=conversation.updated_at
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    conversation = _own_conversation(db, user, conversation_id)
    db.delete(conversation)  # turns and their model-call logs go with it (ON DELETE CASCADE)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
