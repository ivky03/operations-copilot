"""Run the schema-retrieval evaluation on Stage C and write a report.

Free and deterministic: local embeddings, no language model. Usage (from backend/, with
Stage C seeded and published in the database DATABASE_URL points at):

    uv run python -m app.cli seed-reference --stage c
    uv run python -m app.cli publish-reference --stage c
    uv run python -m evals.run_schema_eval            # tuning and held-out sets, k = 3, 5, 8
"""

import argparse
import json
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.db import _session_factory
from app.models import User
from evals.run_answer_eval import PERSONAS, REPORTS, _git
from evals.schema_eval import GOLDEN_SETS, evaluate, load_cases, summarise

KS = (3, 5, 8)


def render_markdown(report: dict) -> str:
    meta = report["meta"]
    out = [
        f"# Schema retrieval: Stage C ({meta['date']}, commit {meta['commit']})",
        "",
        f"{meta['kind']}. Embedding model `{meta['embedding_model']}`. A case is *complete* when",
        "every table it needs is among the tables described to the planner. Tokens are",
        "estimated (characters ÷ 4) for the table descriptions only.",
        "",
    ]
    for name, rows in report["sets"].items():
        out += [
            f"## {name.title()} set ({GOLDEN_SETS[name]})",
            "",
            "| Tables | k | Complete | Tables shown | Description tokens | Full description |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
        for r in rows:
            out.append(
                f"| {r['size']} | {r['k']} | {r['complete']}/{r['cases']} | {r['tables_shown']} "
                f"| {r['context_tokens']:,} | {r['full_tokens']:,} |"
            )
        missed = [(r["size"], r["k"], m) for r in rows for m in r["missed"]]
        if missed:
            out += ["", "Missed:", ""]
            out += [f"- {size} tables, k={k}: {m}" for size, k, m in missed]
        out.append("")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", choices=sorted(GOLDEN_SETS), action="append")
    args = parser.parse_args()
    db = _session_factory()()
    users = {p: db.scalar(select(User).where(User.email == e)) for p, e in PERSONAS.items()}
    if not all(users.values()):
        raise SystemExit("Seed and publish Stage C first (app.cli seed-reference --stage c).")
    sets = {
        name: summarise(evaluate(db, users, load_cases(name), KS))
        for name in args.set or GOLDEN_SETS
    }
    commit = _git("rev-parse", "--short", "HEAD")
    report = {
        "meta": {
            "kind": "Schema retrieval evaluation (deterministic, local embeddings, no model)",
            "date": datetime.now(UTC).date().isoformat(),
            "commit": commit + ("-dirty" if _git("status", "--porcelain") else ""),
            "embedding_model": get_settings().embedding_model,
        },
        "sets": sets,
    }
    stem = f"{report['meta']['date']}-{commit}-stage_c-schema"
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n")
    (REPORTS / f"{stem}.md").write_text(render_markdown(report))
    print(render_markdown(report))
    print(f"→ {REPORTS / stem}.md")


if __name__ == "__main__":
    main()
