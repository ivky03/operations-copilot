"""Run the golden set against the REAL language model and write a scored report.

This is a model-quality evaluation, not a CI test: it uses free-tier quota, results vary from
run to run, and with 45 cases the uncertainty is wide. Each report records the commit, models,
prompt versions and dataset so results are comparable over time.

Usage (from backend/, with the reference data published in the dev database):
    uv run python -m evals.run_answer_eval                 # full run (~25 min on free tiers)
    uv run python -m evals.run_answer_eval --only calc-001 vg-001
    uv run python -m evals.run_answer_eval --golden stage_a.heldout   # held-out questions
"""

import argparse
import json
import subprocess
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.answer import planner, writer
from app.answer.pipeline import answer_question
from app.api.ask import get_planner_llm, get_writer_llm
from app.config import get_settings
from app.db import _session_factory
from app.models import AnswerRun, Conversation, LlmCall, User
from evals.answer_eval import CaseScore, combine_turns, percentile, score_case
from evals.golden import GOLDEN_DIR, load_golden

REPORTS = Path(__file__).resolve().parents[2] / "evals" / "reports"
PERSONAS = {
    "operations": "ops.employee@brindlewood.example",
    "finance": "finance.employee@brindlewood.example",
}


def _git(*args: str) -> str:
    """Read commit info for the report. Fixed command, no user input."""
    cmd = ["git", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout.strip()  # noqa: S603


def _print(n: int, total: int, label: str, score: CaseScore, record: dict) -> None:
    mark = {True: "PASS", False: "FAIL", None: "n/a "}[score.passed]
    detail = "; ".join(score.notes)[:90]
    print(
        f"[{n:>2}/{total}] {label:<9} {mark}  {record['status']:<18} "
        f"{record['latency_ms']:>6} ms  {detail}"
    )


def _run_turn(
    db: Session,
    user: User,
    case: dict,
    turn_id: str,
    question: str,
    expected: dict,
    oracle: list | None,
    conversation: Conversation | None,
) -> tuple[dict, CaseScore]:
    base = {
        "id": turn_id,
        "category": case["category"],
        "persona": case["persona"],
        "question": question,
        "expected": expected,
    }
    started = time.monotonic()
    try:
        r = answer_question(db, user, question, get_planner_llm(), get_writer_llm(), conversation)
    except Exception as e:  # a crash is a failed case, not a failed run
        db.rollback()
        note = f"pipeline crashed: {type(e).__name__}: {str(e)[:150]}"
        score = CaseScore(turn_id, case["category"], scored=True, behaviour_ok=False, notes=[note])
        record = base | {
            "status": "crashed", "answer": "", "notes": [], "options": [], "evidence": [],
            "passages": [], "calls": [], "run_id": None,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "score": score.__dict__ | {"passed": False},
        }  # fmt: skip
        return record, score
    calls = db.scalars(select(LlmCall).where(LlmCall.answer_run_id == r.run_id)).all()
    run = db.get(AnswerRun, r.run_id)
    record = base | {
        "standalone_question": run.plan.get("standalone_question") if run else None,
        "status": r.status,
        "answer": r.answer,
        "notes": r.notes,
        "options": r.options,
        "evidence": [
            {"metric": e.metric, "description": e.description, "rows": e.rows} for e in r.evidence
        ],
        "passages": [
            {"id": p.id, "source": p.source, "version": p.version, "heading": p.heading}
            for p in r.passages
        ],
        "latency_ms": int((time.monotonic() - started) * 1000),
        "calls": [
            {
                "purpose": c.purpose,
                "provider": c.provider,
                "model": c.model,
                "status": c.status,
                "prompt_tokens": c.prompt_tokens,
                "completion_tokens": c.completion_tokens,
                "latency_ms": c.latency_ms,
            }
            for c in calls
        ],
        "run_id": r.run_id,
    }
    score = score_case(turn_id, case["category"], expected, record, oracle)
    record["score"] = score.__dict__ | {"passed": score.passed}
    return record, score


def run(only: list[str] | None, delay: float, golden_name: str = "stage_a") -> dict:
    golden = load_golden(golden_name)
    expected_path = GOLDEN_DIR / f"{golden_name}.expected.json"
    expected_data = json.loads(expected_path.read_text()) if expected_path.exists() else {}
    settings = get_settings()
    db = _session_factory()()
    users = {p: db.scalar(select(User).where(User.email == e)) for p, e in PERSONAS.items()}
    if not all(users.values()):
        raise SystemExit(
            "Seed and publish the reference data first (app.cli seed-reference, publish-reference)."
        )

    results, scores = [], []
    cases = [c for c in golden["cases"] if not only or c["id"] in only]
    for n, case in enumerate(cases, start=1):
        user = users[case["persona"]]
        if "turns" not in case:
            record, score = _run_turn(
                db, user, case, case["id"], case["question"], case["expected"],
                expected_data.get(case["id"]), None,
            )  # fmt: skip
            results.append(record)
            scores.append(score)
            _print(n, len(cases), case["id"], score, record)
            time.sleep(delay)
            continue
        # A follow-up case is one conversation; it passes only if every turn passes.
        conversation = Conversation(user_id=user.id, title=f"eval {case['id']}")
        db.add(conversation)
        db.commit()
        turn_scores = []
        for t, step in enumerate(case["turns"], start=1):
            turn_id = f"{case['id']}#{t}"
            record, score = _run_turn(
                db, user, case, turn_id, step["question"], step["expected"],
                expected_data.get(turn_id), conversation,
            )  # fmt: skip
            results.append(record)
            turn_scores.append(score)
            _print(n, len(cases), turn_id, score, record)
            time.sleep(delay)
        scores.append(combine_turns(case["id"], case["category"], turn_scores))
        # Don't leave evaluation chats in the persona's conversation list. The turns are kept,
        # detached like single-question runs, so the report's run ids still lead to the trace.
        db.execute(
            update(AnswerRun)
            .where(AnswerRun.conversation_id == conversation.id)
            .values(conversation_id=None)
        )
        db.delete(conversation)
        db.commit()

    return {
        "meta": {
            "kind": "model-quality evaluation (real language model; not a CI test)",
            "dataset": golden.get("dataset", "stage_a"),
            "golden": golden_name,
            "golden_version": golden["version"],
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "commit": _git("rev-parse", "--short", "HEAD"),
            "uncommitted_changes": bool(_git("status", "--porcelain", "--", "app", "evals")),
            "planner_model": settings.llm_planner_model,
            "writer_model": settings.llm_writer_model,
            "fallback_model": settings.llm_fallback_model if settings.gemini_api_key else None,
            "prompt_versions": {"planner": planner.PROMPT_VERSION, "writer": writer.PROMPT_VERSION},
            "embedding_model": settings.embedding_model,
            "cases_run": len(cases),
        },
        "results": results,
        "scores": [s.__dict__ | {"passed": s.passed} for s in scores],
    }


def summarise(report: dict) -> dict:
    scores = [CaseScore(**{k: v for k, v in s.items() if k != "passed"}) for s in report["scores"]]
    by_cat: dict[str, list[CaseScore]] = defaultdict(list)
    for s in scores:
        by_cat[s.category].append(s)
    categories = {}
    for cat, items in sorted(by_cat.items()):
        scored = [s for s in items if s.scored]
        data = [s for s in scored if s.data_ok is not None]
        cites = [s for s in scored if s.citations_ok is not None]
        categories[cat] = {
            "cases": len(items),
            "scored": len(scored),
            "passed": sum(1 for s in scored if s.passed),
            "behaviour_ok": sum(1 for s in scored if s.behaviour_ok),
            "data_checked": len(data),
            "data_ok": sum(1 for s in data if s.data_ok),
            "citations_checked": len(cites),
            "citations_ok": sum(1 for s in cites if s.citations_ok),
            "not_scored_reason": items[0].notes[0] if not scored and items[0].notes else None,
        }
    results = report["results"]
    latencies = [r["latency_ms"] for r in results]
    tokens = [
        sum((c["prompt_tokens"] or 0) + (c["completion_tokens"] or 0) for c in r["calls"])
        for r in results
    ]
    answered = [r for r in results if r["status"] in {"answered", "answered_fallback"}]
    providers = Counter(c["provider"] for r in results for c in r["calls"] if c["status"] == "ok")
    errors = Counter(c["status"] for r in results for c in r["calls"] if c["status"] != "ok")
    scored = [s for s in scores if s.scored]
    return {
        "overall": {"scored": len(scored), "passed": sum(1 for s in scored if s.passed)},
        "categories": categories,
        "answers_verified": sum(1 for r in answered if r["status"] == "answered"),
        "answers_fallback": sum(1 for r in answered if r["status"] == "answered_fallback"),
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
        "tokens_per_question": {
            "p50": percentile(tokens, 50),
            "max": max(tokens) if tokens else None,
        },
        "successful_calls_by_provider": dict(providers),
        "failed_calls_by_status": dict(errors),
    }


def render_markdown(report: dict, summary: dict) -> str:
    m = report["meta"]
    o = summary["overall"]
    dirty = " (with uncommitted changes)" if m["uncommitted_changes"] else ""
    prompts = m["prompt_versions"]
    lines = [
        f"# Answer evaluation: {m.get('golden', m['dataset'])} ({m['timestamp'][:10]})",
        "",
        f"> **{m['kind']}.** Results vary between runs; with {m['cases_run']} cases, treat "
        "small differences as noise.",
        "",
        f"- Commit `{m['commit']}`{dirty}, golden set v{m['golden_version']}",
        f"- Planner `{m['planner_model']}`, writer `{m['writer_model']}`, "
        f"fallback `{m['fallback_model']}`",
        f"- Prompts: planner `{prompts['planner']}`, writer `{prompts['writer']}`",
        "",
        f"## Result: {o['passed']} / {o['scored']} scored cases passed",
        "",
        "| Category | Cases | Scored | Passed | Behaviour OK | Data OK / checked "
        "| Citations OK / checked | Not scored because |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cat, c in summary["categories"].items():
        data = f"{c['data_ok']} / {c['data_checked']}" if c["data_checked"] else "–"
        cites = (
            f"{c['citations_ok']} / {c['citations_checked']}" if c.get("citations_checked") else "–"
        )
        lines.append(
            f"| {cat} | {c['cases']} | {c['scored']} | {c['passed']} "
            f"| {c['behaviour_ok']} | {data} | {cites} "
            f"| {c['not_scored_reason'] or ''} |"
        )
    lat, tok = summary["latency_ms"], summary["tokens_per_question"]
    lines += [
        "",
        "## Answer quality and cost",
        "",
        f"- Answers verified on first or second try: **{summary['answers_verified']}**; "
        f"shown as table fallback: **{summary['answers_fallback']}**",
        f"- Latency per question: p50 **{lat['p50']} ms**, p95 **{lat['p95']} ms** "
        "(includes rate-limit waits)",
        f"- Tokens per question: p50 **{tok['p50']}**, max **{tok['max']}**",
        f"- Successful model calls by provider: {summary['successful_calls_by_provider']}",
        f"- Failed or retried calls: {summary['failed_calls_by_status'] or 'none'}",
        "",
        "## Cases that did not pass",
        "",
    ]
    failures = [r for r in report["results"] if r["score"]["passed"] is False]
    if not failures:
        lines.append("None.")
    for r in failures:
        lines += [
            f"### {r['id']} ({r['category']}, {r['persona']})",
            f"- Question: {r['question']}",
            f"- Expected: `{r['expected']['answer']}`; got status `{r['status']}`",
            f"- Notes: {'; '.join(r['score']['notes'])}",
            f"- Evidence: {[e['description'] for e in r['evidence']] or 'none'}",
            f"- Passages: {_passage_labels(r) or 'none'}",
            f"- Answer: {r['answer'][:300].replace(chr(10), ' ')}",
            "",
        ]
    return "\n".join(lines) + "\n"


def _passage_labels(record: dict) -> str:
    return "; ".join(
        f"{p['id']} {p['source']}@v{p['version']} › {p['heading']}"
        for p in record.get("passages", [])
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="*", help="case ids to run")
    parser.add_argument("--delay", type=float, default=3.0, help="seconds between cases")
    parser.add_argument("--golden", default="stage_a", help="golden set name in evals/golden")
    args = parser.parse_args()
    report = run(args.only, args.delay, args.golden)
    summary = summarise(report)
    report["summary"] = summary
    REPORTS.mkdir(parents=True, exist_ok=True)
    stem = f"{report['meta']['timestamp'][:10]}-{report['meta']['commit']}-{args.golden}"
    if args.only:
        stem += "-partial"
    (REPORTS / f"{stem}.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    (REPORTS / f"{stem}.md").write_text(render_markdown(report, summary))
    o = summary["overall"]
    print(f"\n{o['passed']} / {o['scored']} scored cases passed → {REPORTS / stem}.md")


if __name__ == "__main__":
    main()
