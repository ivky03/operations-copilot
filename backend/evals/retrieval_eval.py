"""Retrieval evaluation: does search put the right document sections at the top?

Deterministic (local embeddings, no language model), so it runs in CI as a regression test
as well as producing a report. Pure scoring functions here; `run_retrieval_eval` runs them.

Metrics, over queries that have relevant sections:
- hit@k: share of queries with at least one relevant section in the top k.
- recall@5: share of all relevant sections found in the top 5 (matters when an answer is
  spread over several sections).
- MRR (mean reciprocal rank): 1 / position of the first relevant section, averaged.
  1.0 means always first; 0.5 means typically second.
And over every query:
- leaks: results from a document the persona can't read (must be 0).
- stale: superseded versions returned when history wasn't asked for (must be 0).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from evals.golden import GOLDEN_DIR, load_access, load_documents

PERSONA_GROUPS = {"operations": {"operations"}, "finance": {"finance"}}
LABEL = re.compile(r"^(?P<doc>[a-z0-9-]+)@v(?P<ver>\d+)#(?P<heading>.+)$")
KS = (1, 3, 5)


@dataclass(frozen=True)
class Result:
    """One search result, reduced to what scoring needs."""

    source: str
    version: int
    heading: str
    current: bool

    @property
    def label(self) -> str:
        return f"{self.source}@v{self.version}#{self.heading}"


@dataclass
class QueryScore:
    id: str
    relevant: int
    first_rank: int | None = None  # position of the first relevant result
    found_in_5: int = 0
    leaks: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)


def load_retrieval_golden(path: Path = GOLDEN_DIR / "stage_a.retrieval.yaml") -> dict:
    return yaml.safe_load(path.read_text())


def validate(golden: dict) -> list[str]:
    """Catch mistakes in the labels themselves: unknown documents, versions or headings, or a
    persona labelled with a section it can't read."""
    docs = load_documents()
    access = load_access()
    headings = _document_headings()
    problems = []
    for q in golden["queries"]:
        groups = PERSONA_GROUPS[q["persona"]]
        for label in q["relevant"]:
            m = LABEL.match(label)
            if not m:
                problems.append(f"{q['id']}: bad label {label!r}")
                continue
            doc, ver, heading = m["doc"], int(m["ver"]), m["heading"]
            if ver not in docs.get(doc, set()):
                problems.append(f"{q['id']}: unknown document version {doc}@v{ver}")
            elif heading not in headings[(doc, ver)]:
                problems.append(f"{q['id']}: {doc}@v{ver} has no section {heading!r}")
            if not access.get(doc, set()) & groups:
                problems.append(f"{q['id']}: {q['persona']} cannot read {doc}")
            if ver != max(docs.get(doc, {ver})) and not q.get("history"):
                problems.append(f"{q['id']}: labels an old version without history: true")
    return problems


def _document_headings() -> dict[tuple[str, int], set[str]]:
    from evals.golden import REFERENCE

    out: dict[tuple[str, int], set[str]] = {}
    for path in (REFERENCE / "documents").glob("*.md"):
        _, front, body = path.read_text(encoding="utf-8").split("---", 2)
        meta = yaml.safe_load(front)
        found = set(re.findall(r"^##\s+(.+?)\s*$", body, re.MULTILINE))
        out[(meta["doc_id"], int(meta["version"]))] = found
    return out


def score_query(query: dict, results: list[Result], access: dict[str, set[str]]) -> QueryScore:
    groups = PERSONA_GROUPS[query["persona"]]
    relevant = set(query["relevant"])
    score = QueryScore(query["id"], relevant=len(relevant))
    for position, r in enumerate(results, start=1):
        if not access.get(r.source, set()) & groups:
            score.leaks.append(r.label)
        if not r.current and not query.get("history"):
            score.stale.append(r.label)
        if r.label in relevant:
            if score.first_rank is None:
                score.first_rank = position
            if position <= 5:
                score.found_in_5 += 1
    return score


def summarise(scores: list[QueryScore]) -> dict:
    ranked = [s for s in scores if s.relevant]
    n = len(ranked) or 1
    out = {
        f"hit@{k}": round(
            sum(1 for s in ranked if s.first_rank is not None and s.first_rank <= k) / n, 3
        )
        for k in KS
    }
    out["recall@5"] = round(sum(s.found_in_5 / s.relevant for s in ranked) / n, 3)
    out["mrr"] = round(sum(1 / s.first_rank for s in ranked if s.first_rank) / n, 3)
    out["queries_ranked"] = len(ranked)
    out["leak_probes"] = len(scores) - len(ranked)
    out["leaks"] = sum(len(s.leaks) for s in scores)
    out["stale"] = sum(len(s.stale) for s in scores)
    return out
