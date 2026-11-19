"""Run the retrieval evaluation and write a report comparing keyword, vector and hybrid search.

No language model is involved, so this is free, fast (seconds) and gives the same numbers on
every run for the same data and code.

Usage (from backend/, with the reference data published in the dev database):
    uv run python -m evals.run_retrieval_eval
"""

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import _session_factory
from app.documents.search import Mode, search
from app.models import User
from evals.golden import load_access
from evals.retrieval_eval import (
    QueryScore,
    Result,
    load_retrieval_golden,
    score_query,
    summarise,
)
from evals.run_answer_eval import PERSONAS, REPORTS, _git

MODES: tuple[Mode, ...] = ("keyword", "vector", "hybrid")


def evaluate(db: Session, users: dict[str, User], mode: Mode, k: int = 5) -> list[dict]:
    golden = load_retrieval_golden()
    access = load_access()
    rows = []
    for q in golden["queries"]:
        hits = search(
            db,
            users[q["persona"]],
            q["query"],
            k=k,
            include_superseded=bool(q.get("history")),
            mode=mode,
        )
        results = [Result(h.source, h.version, h.heading, h.current) for h in hits]
        score = score_query(q, results, access)
        rows.append({"query": q, "results": [r.label for r in results], "score": score})
    return rows


def run() -> dict:
    db = _session_factory()()
    users = {p: db.scalar(select(User).where(User.email == e)) for p, e in PERSONAS.items()}
    if not all(users.values()):
        raise SystemExit("Seed and publish the reference data first.")
    by_mode = {mode: evaluate(db, users, mode) for mode in MODES}
    return {
        "meta": {
            "kind": "retrieval evaluation (deterministic; local embeddings, no language model)",
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "commit": _git("rev-parse", "--short", "HEAD"),
            "uncommitted_changes": bool(_git("status", "--porcelain", "--", "app", "evals")),
            "embedding_model": get_settings().embedding_model,
            "k": 5,
        },
        "summary": {m: summarise([r["score"] for r in rows]) for m, rows in by_mode.items()},
        "queries": {
            m: [
                {"id": r["query"]["id"], "results": r["results"], "score": r["score"].__dict__}
                for r in rows
            ]
            for m, rows in by_mode.items()
        },
        "_rows": by_mode,
    }


def render_markdown(report: dict) -> str:
    m, s = report["meta"], report["summary"]
    dirty = " (with uncommitted changes)" if m["uncommitted_changes"] else ""
    lines = [
        f"# Retrieval evaluation: stage_a documents ({m['timestamp'][:10]})",
        "",
        f"> **{m['kind']}.** Same data and code give the same numbers.",
        "",
        f"- Commit `{m['commit']}`{dirty}; embedding model `{m['embedding_model']}`; top {m['k']}",
        f"- {s['hybrid']['queries_ranked']} ranked queries + "
        f"{s['hybrid']['leak_probes']} leak probes",
        "",
        "| Mode | hit@1 | hit@3 | hit@5 | recall@5 | MRR | leaks | stale |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for mode, v in s.items():
        lines.append(
            f"| {mode} | {v['hit@1']} | {v['hit@3']} | {v['hit@5']} | {v['recall@5']} "
            f"| {v['mrr']} | {v['leaks']} | {v['stale']} |"
        )
    lines += ["", "## Hybrid: queries where the first relevant section was not ranked 1st", ""]
    misses = [r for r in report["_rows"]["hybrid"] if _missed(r["score"])]
    if not misses:
        lines.append("None.")
    for r in misses:
        q, sc = r["query"], r["score"]
        where = f"rank {sc.first_rank}" if sc.first_rank else "not in top 5"
        lines += [
            f"- **{q['id']}** ({q['persona']}) “{q['query']}”: {where}",
            f"  - wanted: {', '.join(q['relevant'])}",
            f"  - got: {', '.join(r['results'][:3])}",
        ]
    return "\n".join(lines) + "\n"


def _missed(score: QueryScore) -> bool:
    return bool(score.relevant) and score.first_rank != 1


def main() -> None:
    report = run()
    REPORTS.mkdir(parents=True, exist_ok=True)
    stem = f"{report['meta']['timestamp'][:10]}-{report['meta']['commit']}-stage_a-retrieval"
    markdown = render_markdown(report)
    report.pop("_rows")
    (REPORTS / f"{stem}.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    (REPORTS / f"{stem}.md").write_text(markdown)
    print(markdown)


if __name__ == "__main__":
    main()
