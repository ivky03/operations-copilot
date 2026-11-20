"""The answer pipeline: a fixed sequence of steps, not an autonomous agent.

    question
      → plan (LLM, ≤2 calls: one retry if the plan names something not in the catalog);
        in a conversation the planner also sees the last turns and restates a follow-up
        ("And April?") as a complete question, used by every later step
      → ground filter values ("UK" → "United Kingdom"; ambiguous → ask the user)
      → compile + run each query (read-only, as M3a)
      → search documents the user can read (hybrid, permission-filtered, as M4a)
      → write (LLM, ≤2 calls: one retry with feedback if verification fails)
      → verify numbers and citations; if still failing, show the evidence as-is
      → save the full trace (answer_runs, llm_calls)

At most 4 model calls per question, so latency and cost are bounded and every step is testable.
"""

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.answer.context import build_context
from app.answer.evidence import EvidenceItem, build_evidence
from app.answer.history import prior_turns, render
from app.answer.passages import Passage, document_titles, retrieve_passages
from app.answer.planner import AnswerPlan, plan_question
from app.answer.verify import cited_ids, normalise_citations, verify
from app.answer.writer import template_answer, write_answer
from app.llm.client import CallRecord, LLMClient, LLMOutputError, LLMUnavailableError
from app.models import AnswerRun, Conversation, LlmCall, User
from app.query.catalog import Catalog, PlanError
from app.query.compiler import compile_plan
from app.query.executor import QueryTimeoutError, execute
from app.query.grounding import ground
from app.query.plan import QueryPlan


@dataclass
class AnswerResult:
    run_id: str
    question: str
    status: str  # answered | answered_fallback | clarification | unanswerable | error
    answer: str
    options: list[str] = field(default_factory=list)  # choices offered with a clarification
    evidence: list[EvidenceItem] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
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
            note = None
            if g.match.value != phrase:
                note = f"Interpreted “{phrase}” as “{g.match.value}”."
            elif not set(_words(g.match.value)) & set(_words(question)):
                label = resolved.column.name.replace("_", " ")
                note = f"Interpreted your question as {label} “{g.match.value}”."
            if note and note not in notes:  # several queries may ground the same value
                notes.append(note)
            values.append(g.match.value)
        new_filters.append(f.model_copy(update={"values": values}))
    return plan.model_copy(update={"filters": new_filters})


def _plan_with_retry(
    llm: LLMClient,
    db: Session,
    user: User,
    catalog: Catalog,
    question: str,
    calls: list[CallRecord],
    history: str = "",
) -> AnswerPlan:
    context = build_context(db, catalog)
    documents = document_titles(db, user)
    plan = plan_question(llm, question, context, calls, documents, history)
    for query in plan.queries:
        try:
            compile_plan(query, catalog)  # dry run: are all names in the catalog?
        except PlanError as e:
            retry_q = f"{question}\n\n(Your previous plan was invalid: {e} Use only listed names.)"
            return plan_question(llm, retry_q, context, calls, documents, history)
    return plan


NOT_IN_DOCUMENTS = "I couldn't find anything about that in the documents you have access to."


def answer_question(
    db: Session,
    user: User,
    question: str,
    planner: LLMClient,
    writer: LLMClient,
    conversation: Conversation | None = None,
) -> AnswerResult:
    start = time.monotonic()
    run_id = uuid.uuid4()
    calls: list[CallRecord] = []
    notes: list[str] = []
    evidence: list[EvidenceItem] = []
    passages: list[Passage] = []
    plan: AnswerPlan | None = None
    verification: dict = {}
    status, answer, options = "error", "", []
    standalone = question
    try:
        catalog = Catalog.for_user(db, user)
        history = render(prior_turns(db, user, conversation.id)) if conversation else ""
        plan = _plan_with_retry(planner, db, user, catalog, question, calls, history)
        if history and plan.standalone_question:
            standalone = plan.standalone_question  # "And April?" → "Net sales by country, April"
        if plan.kind == "clarify":
            raise _Stop("clarification", plan.clarification or "Could you clarify your question?")
        if plan.kind == "unanswerable":
            raise _Stop(
                "unanswerable",
                "I can't answer that from the data you have access to"
                + (f": {plan.reason}" if plan.reason else "."),
            )
        queries = plan.queries if plan.kind == "data" else []
        searches = plan.doc_searches or ([] if queries else [standalone])
        for i, query in enumerate(queries, start=1):
            grounded = _ground_plan(db, catalog, query, standalone, notes)
            compiled = compile_plan(grounded, catalog)
            evidence.append(build_evidence(i, grounded, compiled, execute(compiled)))
        if searches:
            passages = retrieve_passages(db, user, searches, plan.include_history)
        if not evidence and not passages:
            raise _Stop("unanswerable", NOT_IN_DOCUMENTS)

        draft = write_answer(writer, standalone, evidence, notes, calls, passages=passages)
        if not draft.answerable and not evidence:
            # The writer read the passages and found nothing relevant. Say so in our own words
            # rather than showing model text that can't be checked against a citation.
            verification = {"ok": True, "writer_found_no_answer": True}
            raise _Stop("unanswerable", NOT_IN_DOCUMENTS)
        text = normalise_citations(draft.answer)
        asked = f"{question} {standalone}"  # numbers the user typed, either way
        check = verify(text, evidence, asked, passages)
        rejected = []
        if not check.ok:
            rejected.append({"draft": text, "problems": check.feedback()})
            text = normalise_citations(
                write_answer(
                    writer, standalone, evidence, notes, calls, check.feedback(), passages
                ).answer
            )
            check = verify(text, evidence, asked, passages)
            if not check.ok:
                rejected.append({"draft": text, "problems": check.feedback()})
        verification = {
            "ok": check.ok,
            "unsupported_numbers": check.unsupported_numbers,
            "unknown_citations": check.unknown_citations,
            "missing_citation": check.missing_citation,
            "cited": sorted(cited_ids(text)) if check.ok else [],
            "rejected_drafts": rejected,  # kept for diagnosis; never shown to the user
        }
        if check.ok:
            status, answer = "answered", text
        else:
            status, answer = "answered_fallback", template_answer(evidence, notes, passages)
    except _Stop as stop:
        status, answer, options = stop.status, stop.answer, stop.options
    except (PlanError, QueryTimeoutError) as e:
        status, answer = "unanswerable", str(e)
    except LLMUnavailableError as e:
        status, answer = "error", f"{e} Please try again shortly."
    except LLMOutputError:
        status, answer = "error", "I couldn't interpret that question reliably. Try rephrasing it."

    sources = sorted(
        {(s["source"], s["version"]) for e in evidence for s in e.sources}
        | {(p.source, p.version) for p in passages}
    )
    latency = int((time.monotonic() - start) * 1000)
    db.add(
        AnswerRun(
            id=run_id,
            user_id=user.id,
            conversation_id=conversation.id if conversation else None,
            question=question,
            status=status,
            answer=answer,
            plan=plan.model_dump(mode="json") if plan else {},
            evidence=[e.as_dict() for e in evidence],
            passages=[p.as_dict() for p in passages],
            source_versions=[{"source": s, "version": v} for s, v in sources],
            verification={**verification, "notes": notes, "options": options},
            latency_ms=latency,
        )
    )
    if conversation:
        conversation.updated_at = datetime.now(UTC)
    db.flush()
    db.add_all(LlmCall(answer_run_id=run_id, **c.__dict__) for c in calls)
    db.commit()
    return AnswerResult(
        run_id=str(run_id),
        question=question,
        status=status,
        answer=answer,
        options=options,
        evidence=evidence,
        passages=passages,
        notes=notes,
        sources=[{"source": s, "version": v} for s, v in sources],
        latency_ms=latency,
    )
