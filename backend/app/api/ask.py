"""POST /api/ask: ask a question in plain language; get an answer with its evidence.

Single-turn for now (conversations arrive in M5). The language-model clients are FastAPI
dependencies so tests can replace them with a scripted fake.
"""

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.answer.pipeline import answer_question
from app.auth import current_user
from app.config import get_settings
from app.db import get_db
from app.llm.client import LLMClient, build_client
from app.models import User

router = APIRouter(prefix="/api", tags=["ask"])


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


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
    run_id: str
    status: str
    answer: str
    options: list[str]
    notes: list[str]
    evidence: list[EvidenceOut]
    passages: list[PassageOut]
    sources: list[dict[str, str | int]]
    latency_ms: int


def get_planner_llm() -> LLMClient:
    return build_client(purpose_model=get_settings().llm_planner_model)


def get_writer_llm() -> LLMClient:
    return build_client(purpose_model=get_settings().llm_writer_model)


@router.post("/ask")
async def ask(
    body: AskRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    planner: LLMClient = Depends(get_planner_llm),
    writer: LLMClient = Depends(get_writer_llm),
) -> AskResponse:
    # The pipeline makes blocking network and database calls: keep them off the event loop.
    result = await run_in_threadpool(answer_question, db, user, body.question, planner, writer)
    return AskResponse(
        run_id=result.run_id,
        status=result.status,
        answer=result.answer,
        options=result.options,
        notes=result.notes,
        evidence=[EvidenceOut(**{k: v for k, v in e.as_dict().items()}) for e in result.evidence],
        passages=[PassageOut(**p.as_dict()) for p in result.passages],
        sources=result.sources,
        latency_ms=result.latency_ms,
    )
