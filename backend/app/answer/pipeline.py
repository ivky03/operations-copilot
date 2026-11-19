"""The answer pipeline: a fixed sequence of steps, not an autonomous agent.

    question
      → plan (LLM, ≤2 calls: one retry if the plan names something not in the catalog)
      → ground filter values ("UK" → "United Kingdom"; ambiguous → ask the user)
      → compile + run each query (read-only, as M3a)
      → write (LLM, ≤2 calls: one retry with feedback if verification fails)
      → verify numbers and citations; if still failing, show the evidence as tables
      → save the full trace (answer_runs, llm_calls)

At most 4 model calls per question, so latency and cost are bounded and every step is testable.
"""

import re
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.answer.context import build_context
from app.answer.evidence import EvidenceItem, build_evidence
from app.answer.planner import AnswerPlan, plan_question
from app.answer.verify import verify
from app.answer.writer import template_answer, write_answer
from app.llm.client import CallRecord, LLMClient, LLMOutputError, LLMUnavailableError
from app.models import AnswerRun, LlmCall, User
from app.query.catalog import Catalog, PlanError
from app.query.compiler import compile_plan
from app.query.executor import QueryTimeoutError, execute
from app.query.grounding import ground
from app.query.plan import QueryPlan


@dataclass
class AnswerResult:
    run_id: str
    status: str  # answered | answered_fallback | clarification | unanswerable | documents | error
    answer: str
    options: list[str] = field(default_factory=list)  # choices offered with a clarification
    evidence: list[EvidenceItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    latency_ms: int = 0


class _Stop(Exception):  # noqa: N818 - control flow: end the pipeline early with a result
    def __init__(self, status: str, answer: str, options: list[str] | None = None):
        self.status, self.answer, self.options = status, answer, options or []


def _words(text: str) -> list[str]:
    """Lowercase words with a trailing plural 's' removed ("Lamps" → "lamp")."""
    return [
        w[:-1] if len(w) > 3 and w.endswith("s") else w
        for w in re.findall(r"[a-z0-9]+", text.lower())
    ]


def _user_phrase(value: str, question: str) -> str | None:
    """If the planner *narrowed* the user's words (question "desk lamps" → value "Blue Desk
    Lamp"), return the user's own phrase ("desk lamp") so grounding can detect ambiguity.
    Returns None when the value is the user's phrase, or shares no words with the question
    (an interpretation such as "UK" → "United Kingdom")."""
    value_words, question_words = _words(value), set(_words(question))
    shared = [w for w in value_words if w in question_words]
    if not shared or len(shared) == len(value_words):
        return None
    return " ".join(shared)


def _ground_plan(
    db: Session, catalog: Catalog, plan: QueryPlan, question: str, notes: list[str]
) -> QueryPlan:
    """Replace filter values with real values from the data, or stop to ask / explain."""
    dataset, _ = catalog.find_metric(plan.metric)
    new_filters = []
    for f in plan.filters:
        resolved = catalog.resolve_dimension(dataset, f.dimension)
        if not resolved.column.value_index:
            new_filters.append(f)
            continue
        values = []
        for planned in f.values:
            # Don't trust a planner that resolved an ambiguous phrase on its own.
            phrase = _user_phrase(planned, question) or planned
            g = ground(db, catalog, phrase, (resolved.dataset.slug, resolved.column.name))
            if g.status == "ambiguous":
                options = [c.value for c in g.candidates]
                raise _Stop(
                    "clarification",
                    f"“{phrase}” matches several {resolved.column.name.replace('_', ' ')} values. "
                    f"Which one do you mean: {', '.join(options)}?",
                    options,
                )
            if g.status == "not_found":
                raise _Stop(
                    "unanswerable",
                    f"I couldn't find “{phrase}” in {resolved.dataset.slug} "
                    f"({resolved.column.name.replace('_', ' ')}), so I can't answer that.",
                )
            if g.match.value != phrase:
                notes.append(f"Interpreted “{phrase}” as “{g.match.value}”.")
            elif not set(_words(g.match.value)) & set(_words(question)):
                label = resolved.column.name.replace("_", " ")
                notes.append(f"Interpreted your question as {label} “{g.match.value}”.")
            values.append(g.match.value)
        new_filters.append(f.model_copy(update={"values": values}))
    return plan.model_copy(update={"filters": new_filters})


def _plan_with_retry(
    llm: LLMClient, db: Session, catalog: Catalog, question: str, calls: list[CallRecord]
) -> AnswerPlan:
    context = build_context(db, catalog)
    plan = plan_question(llm, question, context, calls)
    for query in plan.queries:
        try:
            compile_plan(query, catalog)  # dry run: are all names in the catalog?
        except PlanError as e:
            retry_q = f"{question}\n\n(Your previous plan was invalid: {e} Use only listed names.)"
            return plan_question(llm, retry_q, context, calls)
    return plan


def answer_question(
    db: Session, user: User, question: str, planner: LLMClient, writer: LLMClient
) -> AnswerResult:
    start = time.monotonic()
    run_id = uuid.uuid4()
    calls: list[CallRecord] = []
    notes: list[str] = []
    evidence: list[EvidenceItem] = []
    plan: AnswerPlan | None = None
    verification: dict = {}
    status, answer, options = "error", "", []
    try:
        catalog = Catalog.for_user(db, user)
        plan = _plan_with_retry(planner, db, catalog, question, calls)
        if plan.kind == "clarify":
            raise _Stop("clarification", plan.clarification or "Could you clarify your question?")
        if plan.kind == "unanswerable":
            raise _Stop(
                "unanswerable",
                "I can't answer that from the data you have access to"
                + (f": {plan.reason}" if plan.reason else "."),
            )
        if plan.kind == "documents" or not plan.queries:
            raise _Stop(
                "documents",
                "That's a question about policies or definitions. Document answers aren't "
                "available yet; numeric questions are.",
            )
        for i, query in enumerate(plan.queries, start=1):
            grounded = _ground_plan(db, catalog, query, question, notes)
            compiled = compile_plan(grounded, catalog)
            evidence.append(build_evidence(i, grounded, compiled, execute(compiled)))

        text = write_answer(writer, question, evidence, notes, calls)
        check = verify(text, evidence, question)
        if not check.ok:
            text = write_answer(writer, question, evidence, notes, calls, feedback=check.feedback())
            check = verify(text, evidence, question)
        verification = {
            "ok": check.ok,
            "unsupported_numbers": check.unsupported_numbers,
            "unknown_citations": check.unknown_citations,
            "missing_citation": check.missing_citation,
        }
        if check.ok:
            status, answer = "answered", text
        else:
            status, answer = "answered_fallback", template_answer(evidence, notes)
    except _Stop as stop:
        status, answer, options = stop.status, stop.answer, stop.options
    except (PlanError, QueryTimeoutError) as e:
        status, answer = "unanswerable", str(e)
    except LLMUnavailableError as e:
        status, answer = "error", f"{e} Please try again shortly."
    except LLMOutputError:
        status, answer = "error", "I couldn't interpret that question reliably. Try rephrasing it."

    sources = sorted({(s["source"], s["version"]) for e in evidence for s in e.sources})
    latency = int((time.monotonic() - start) * 1000)
    db.add(
        AnswerRun(
            id=run_id,
            user_id=user.id,
            question=question,
            status=status,
            answer=answer,
            plan=plan.model_dump(mode="json") if plan else {},
            evidence=[e.as_dict() for e in evidence],
            source_versions=[{"source": s, "version": v} for s, v in sources],
            verification={**verification, "notes": notes},
            latency_ms=latency,
        )
    )
    db.flush()
    db.add_all(LlmCall(answer_run_id=run_id, **c.__dict__) for c in calls)
    db.commit()
    return AnswerResult(
        run_id=str(run_id),
        status=status,
        answer=answer,
        options=options,
        evidence=evidence,
        notes=notes,
        sources=[{"source": s, "version": v} for s, v in sources],
        latency_ms=latency,
    )
