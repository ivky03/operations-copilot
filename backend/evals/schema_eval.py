"""Schema-retrieval evaluation: does retrieval find every table a question needs?

No language model is involved, so it's free, takes seconds and gives the same numbers every
run. Each golden case names the tables a correct plan must use (`tables`). For each
experiment size (4, 15, 30 or 50 tables) that contains them, and each k, retrieval runs over
the persona's catalog cut down to that size, and the case scores:

- complete: every needed table is among those shown to the planner. This is what matters: a
  missing table can't be planned with, however good the model.
- tables shown, and the estimated size of the description the planner reads, against the
  full description of every table at that size.

Token counts are estimated as characters ÷ 4, the usual rule of thumb for English with these
models' tokenizers; the M8c experiment records the providers' real counts.
"""

from dataclasses import dataclass
from pathlib import Path
from statistics import mean

import yaml
from sqlalchemy.orm import Session

from app.answer.context import build_context
from app.models import User
from app.query.catalog import Catalog
from app.query.schema_retrieval import select_tables
from datagen.stage_c import SIZE_30
from evals.golden import GOLDEN_DIR

DATA = Path(__file__).resolve().parents[2] / "data"
SIZES = (4, 15, 30, 50)
GOLDEN_SETS = {"tuning": "stage_c.yaml", "heldout": "stage_c.schema_heldout.yaml"}


def experiment_sizes() -> dict[int, set[str]]:
    """The tables at each step of the scale experiment: Stage A, + Stage B, + SIZE_30, all."""

    def stems(stage: str) -> set[str]:
        return {p.stem for p in (DATA / stage / "manifests").glob("*.yaml")}

    a, b, c = stems("reference"), stems("stage_b"), stems("stage_c")
    sizes = {4: a, 15: a | b, 30: a | b | set(SIZE_30), 50: a | b | c}
    assert [len(sizes[n]) for n in SIZES] == list(SIZES), "experiment sizes changed"
    return sizes


def load_cases(name: str) -> list[dict]:
    return yaml.safe_load((GOLDEN_DIR / GOLDEN_SETS[name]).read_text())["cases"]


def tokens(text: str) -> int:
    return round(len(text) / 4)


@dataclass
class CaseResult:
    case_id: str
    size: int
    k: int
    needed: list[str]
    shown: list[str]  # every table described to the planner
    missing: list[str]
    context_tokens: int
    full_tokens: int

    @property
    def complete(self) -> bool:
        return not self.missing


class ContextSizes:
    """Building a description runs a few small queries, so each distinct one is built once."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self._cache: dict[tuple, int] = {}

    def __call__(self, catalog: Catalog, only: set[str] | None) -> int:
        key = (frozenset(catalog.datasets), frozenset(only) if only is not None else None)
        if key not in self._cache:
            self._cache[key] = tokens(build_context(self.db, catalog, only).text)
        return self._cache[key]


def evaluate(
    db: Session, users: dict[str, User], cases: list[dict], ks: tuple[int, ...]
) -> list[CaseResult]:
    sizes = experiment_sizes()
    catalogs = {p: Catalog.for_user(db, u) for p, u in users.items()}
    context_tokens = ContextSizes(db)
    results = []
    for case in cases:
        for size in SIZES:
            full = catalogs[case["persona"]]
            catalog = Catalog({s: d for s, d in full.datasets.items() if s in sizes[size]})
            if not set(case["tables"]) <= set(catalog.datasets):
                continue  # not answerable at this size (or not by this persona)
            for k in ks:
                shown = select_tables(db, catalog, [case["question"]], k).tables
                results.append(
                    CaseResult(
                        case_id=case["id"],
                        size=size,
                        k=k,
                        needed=sorted(case["tables"]),
                        shown=sorted(shown),
                        missing=sorted(set(case["tables"]) - shown),
                        context_tokens=context_tokens(catalog, shown),
                        full_tokens=context_tokens(catalog, None),
                    )
                )
    return results


def summarise(results: list[CaseResult]) -> list[dict]:
    """One row per (size, k): how many cases had every table found, and what it cost."""
    rows = []
    for size in SIZES:
        for k in sorted({r.k for r in results}):
            group = [r for r in results if r.size == size and r.k == k]
            if not group:
                continue
            rows.append(
                {
                    "size": size,
                    "k": k,
                    "cases": len(group),
                    "complete": sum(r.complete for r in group),
                    "tables_shown": round(mean(len(r.shown) for r in group), 1),
                    "context_tokens": round(mean(r.context_tokens for r in group)),
                    "full_tokens": round(mean(r.full_tokens for r in group)),
                    "missed": [
                        f"{r.case_id} (needs {', '.join(r.missing)})" for r in group if r.missing
                    ],
                }
            )
    return rows
