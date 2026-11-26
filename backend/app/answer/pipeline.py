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
With `use_cache`, a first question whose answer is already known for exactly the same readable
source versions is answered from the cache (app/answer/cache.py) with no model calls at all.

Progress events (`on_event`) report each step as it finishes, with the code-computed data and
the passages as soon as they exist, so a streaming client can show them early. The answer text
itself is only ever reported after verification: unchecked model text never leaves this module.
"""

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.answer import cache
from app.answer.context import build_context
from app.answer.evidence import EvidenceItem, build_evidence
from app.answer.history import prior_turns, render
from app.answer.passages import Passage, document_titles, retrieve_passages
from app.answer.planner import AnswerPlan, plan_question
from app.answer.verify import cited_ids, normalise_citations, verify
from app.answer.writer import template_answer, write_answer
from app.ingest.publish import DATASET_SCHEMA
from app.llm.client import CallRecord, LLMClient, LLMOutputError, LLMUnavailableError
from app.models import AnswerRun, Conversation, LlmCall, User
from app.query.catalog import Catalog, PlanError
from app.query.compiler import compile_plan, q
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
    standalone_question: str | None = None  # how a follow-up was understood, if restated
    evidence: list[EvidenceItem] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    latency_ms: int = 0
    cached: bool = False  # reused from an identical earlier question (same versions readable)


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


def _missing_values(db: Session, resolved, f) -> list[str]:
    """Filter values that appear nowhere in the column (compared as text)."""
    table = f"{DATASET_SCHEMA}.{q(resolved.dataset.table)}"
    column = q(resolved.column.name)
    found = set(
        db.scalars(
            text(  # identifiers come from the catalog, values are bound
                f"SELECT DISTINCT {column}::text FROM {table} "  # noqa: S608
                f"WHERE {column}::text = ANY(:values)"
            ),
            {"values": list(f.values)},
        )
    )
    return [v for v in f.values if v not in found]


def _ground_plan(
    db: Session, catalog: Catalog, plan: QueryPlan, question: str, notes: list[str]
) -> QueryPlan:
    """Replace filter values with real values from the data, or stop to ask / explain."""
    dataset, _ = catalog.find_metric(plan.metric)
    new_filters = []
    for f in plan.filters:
        resolved = catalog.resolve_dimension(dataset, f.dimension)
        if not resolved.column.value_index:
            # No value index to ground against (ids, dates, flags): at least make sure the value
            # exists, or a made-up value ("oak tables" as a product id) silently matches nothing
            # and the answer reports zero.
            missing = _missing_values(db, resolved, f) if f.op in {"=", "in"} else []
            if missing:
                label = resolved.column.name.replace("_", " ")
                raise _Stop(
                    "unanswerable",
                    f"I couldn't find {', '.join(f'“{v}”' for v in missing)} in "
                    f"{resolved.dataset.slug} ({label}), so I can't answer that.",
                )
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


def _replay(
    db: Session,
    user: User,
    question: str,
    hit: AnswerRun,
    conversation: Conversation | None,
    emit: Callable[..., None],
    start: float,
) -> AnswerResult:
    """Answer from the cache: the same events a fresh answer sends, then a new saved run for
    this user (a copy, so it's erased with the original if a source it used is deleted)."""
    emit("step", step="cached")
    evidence = [EvidenceItem(**e) for e in hit.evidence]
    passages = [Passage(**p) for p in hit.passages]
    notes = hit.verification.get("notes", [])
    if evidence:
        emit("evidence", items=[e.as_dict() for e in evidence], notes=notes)
    if passages:
        emit("passages", items=[p.as_dict() for p in passages])
    latency = int((time.monotonic() - start) * 1000)
    run = AnswerRun(
        user_id=user.id,
        conversation_id=conversation.id if conversation else None,
        question=question,
        status=hit.status,
        answer=hit.answer,
        plan=hit.plan,
        evidence=hit.evidence,
        passages=hit.passages,
        source_versions=hit.source_versions,
        verification=hit.verification,
        latency_ms=latency,
        cached_from=hit.cached_from or hit.id,
    )
    db.add(run)
    if conversation:
        conversation.updated_at = datetime.now(UTC)
    db.commit()
    return AnswerResult(
        run_id=str(run.id),
        question=question,
        status=hit.status,
        answer=hit.answer or "",
        options=hit.verification.get("options", []),
        evidence=evidence,
        passages=passages,
        notes=notes,
        sources=hit.source_versions,
        latency_ms=latency,
        cached=True,
    )


def answer_question(
    db: Session,
    user: User,
    question: str,
    planner: LLMClient,
    writer: LLMClient,
    conversation: Conversation | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    use_cache: bool = False,
) -> AnswerResult:
    def emit(event: str, **data) -> None:
        if on_event:
            on_event(event, data)

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
    key: str | None = None
    try:
        history = render(prior_turns(db, user, conversation.id)) if conversation else ""
        if use_cache and not history:  # follow-ups depend on the conversation: never cached
            readable = cache.readable_versions(db, user)
            key = cache.cache_key(question, readable, planner, writer)
            if (hit := cache.lookup(db, key, readable)) is not None:
                return _replay(db, user, question, hit, conversation, emit, start)
        emit("step", step="planning")
        catalog = Catalog.for_user(db, user)
        plan = _plan_with_retry(planner, db, user, catalog, question, calls, history)
        if history and plan.standalone_question:
            standalone = plan.standalone_question  # "And April?" → "Net sales by country, April"
        emit(
            "planned",
            kind=plan.kind,
            queries=len(plan.queries) if plan.kind == "data" else 0,
            doc_searches=plan.doc_searches,
            standalone_question=standalone if standalone != question else None,
        )
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
        if queries:
            emit("step", step="querying")
        for i, query in enumerate(queries, start=1):
            grounded = _ground_plan(db, catalog, query, standalone, notes)
            compiled = compile_plan(grounded, catalog)
            evidence.append(build_evidence(i, grounded, compiled, execute(compiled)))
        if evidence:
            emit("evidence", items=[e.as_dict() for e in evidence], notes=notes)
        if searches:
            emit("step", step="searching")
            passages = retrieve_passages(db, user, searches, plan.include_history)
            emit("passages", items=[p.as_dict() for p in passages])
        if not evidence and not passages:
            raise _Stop("unanswerable", NOT_IN_DOCUMENTS)

        emit("step", step="writing")
        draft = write_answer(writer, standalone, evidence, notes, calls, passages=passages)
        if not draft.answerable and not evidence:
            # The writer read the passages and found nothing relevant. Say so in our own words
            # rather than showing model text that can't be checked against a citation.
            verification = {"ok": True, "writer_found_no_answer": True}
            raise _Stop("unanswerable", NOT_IN_DOCUMENTS)
        text = normalise_citations(draft.answer)
        asked = f"{question} {standalone}"  # numbers the user typed, either way
        emit("step", step="verifying")
        check = verify(text, evidence, asked, passages)
        rejected = []
        if not check.ok:
            rejected.append({"draft": text, "problems": check.feedback()})
            emit("step", step="rewriting")  # the problems themselves stay in the trace
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
    if key and status == "answered":
        cache.store(db, key, run_id)
    db.commit()
    return AnswerResult(
        run_id=str(run_id),
        question=question,
        status=status,
        standalone_question=standalone if standalone != question else None,
        answer=answer,
        options=options,
        evidence=evidence,
        passages=passages,
        notes=notes,
        sources=[{"source": s, "version": v} for s, v in sources],
        latency_ms=latency,
    )
