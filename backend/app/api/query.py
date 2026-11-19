"""Structured query endpoints.

POST /api/query   run a QueryPlan against the caller's catalog and return rows, the SQL and the
                  dataset versions used (the "evidence" a final answer will cite)
POST /api/ground  map a phrase to real values ("UK" → United Kingdom), or report ambiguity

Both are scoped to the datasets the caller can read. The answer pipeline (M3b) calls the same
functions; these endpoints make the engine directly testable and inspectable.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import current_user
from app.db import get_db
from app.models import User
from app.query.catalog import Catalog, PlanError
from app.query.compiler import compile_plan
from app.query.executor import QueryTimeoutError, execute
from app.query.grounding import ground
from app.query.plan import QueryPlan

router = APIRouter(prefix="/api", tags=["query"])


class SourceVersionOut(BaseModel):
    source: str
    version: int


class QueryOut(BaseModel):
    metric: str
    title: str
    unit: str | None
    columns: list[str]
    rows: list[dict[str, str | float | int | None]]
    sql: str
    sources: list[SourceVersionOut]


class GroundRequest(BaseModel):
    phrase: str
    dataset: str | None = None
    column: str | None = None


class CandidateOut(BaseModel):
    dataset: str
    column: str
    value: str
    score: float


class GroundOut(BaseModel):
    phrase: str
    status: str
    match: CandidateOut | None
    candidates: list[CandidateOut]


@router.post("/query")
async def run_query(
    plan: QueryPlan, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> QueryOut:
    try:
        compiled = compile_plan(plan, Catalog.for_user(db, user))
        result = await run_in_threadpool(execute, compiled)
    except PlanError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    except QueryTimeoutError as e:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(e)) from e
    return QueryOut(
        metric=compiled.metric,
        title=compiled.title,
        unit=compiled.unit,
        columns=result.columns,
        rows=result.rows,
        sql=compiled.sql,
        sources=[SourceVersionOut(source=s.slug, version=s.version) for s in compiled.sources],
    )


@router.post("/ground")
def ground_phrase(
    body: GroundRequest, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> GroundOut:
    if (body.dataset is None) != (body.column is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Give both dataset and column, or neither."
        )
    column = (body.dataset, body.column) if body.dataset and body.column else None
    g = ground(db, Catalog.for_user(db, user), body.phrase, column)

    def out(c):
        return CandidateOut(dataset=c.dataset, column=c.column, value=c.value, score=c.score)

    return GroundOut(
        phrase=g.phrase,
        status=g.status,
        match=out(g.match) if g.match else None,
        candidates=[out(c) for c in g.candidates],
    )
