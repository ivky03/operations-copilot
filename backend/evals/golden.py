"""Load and validate the golden evaluation set, and compute its expected numeric answers.

Validation catches mistakes in the test set itself (an exam with a wrong answer key is worse
than no exam):
- every cited document and version exists;
- a persona can read what it must cite, and cannot read what it must not see in
  "restricted" cases;
- clarification options and grounded values exist in the data;
- every data query is valid for the oracle.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from evals.oracle import METRICS, Oracle, Query

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "data" / "reference"
STAGE_B = ROOT / "data" / "stage_b"
STAGE_C = ROOT / "data" / "stage_c"
# A golden set's `dataset` names the data stage it is written for. Answerability depends on
# the data: "how many people work in Berlin?" has no answer in Stage A and is 5 in Stage B.
STAGE_ROOTS = {
    "stage_a": [REFERENCE],
    "stage_b": [REFERENCE, STAGE_B],
    "stage_c": [REFERENCE, STAGE_B, STAGE_C],
}
GOLDEN_DIR = ROOT / "evals" / "golden"
CATEGORIES = {
    "calculation",
    "value_grounding",
    "policy",
    "combined",
    "ambiguous",
    "unanswerable",
    "permission",
    "follow_up",
    "table_choice",  # look-alike tables or metrics: the question must reach the right one
    "now_answerable",  # unanswerable at an earlier stage, answerable with this stage's data
}
ANSWER_TYPES = {"answer", "clarify", "abstain", "restricted"}


@dataclass(frozen=True)
class DocVersion:
    doc_id: str
    version: int


def load_documents(stage: str = "stage_a") -> dict[str, set[int]]:
    """doc_id -> set of versions, read from each document's front matter."""
    docs: dict[str, set[int]] = {}
    paths = [p for root in STAGE_ROOTS[stage] for p in sorted((root / "documents").glob("*.md"))]
    for path in paths:
        front = path.read_text(encoding="utf-8").split("---")[1]
        meta = yaml.safe_load(front)
        docs.setdefault(meta["doc_id"], set()).add(int(meta["version"]))
    return docs


def load_access(stage: str = "stage_a") -> dict[str, set[str]]:
    """source -> groups allowed to read it."""
    access: dict[str, set[str]] = {}
    for root in STAGE_ROOTS[stage]:
        grants = yaml.safe_load((root / "access.yaml").read_text())["grants"]
        access |= {k: set(v) for k, v in grants.items()}
    return access


def load_sources(stage: str) -> set[str]:
    """Every dataset and document a data stage publishes."""
    return set(load_access(stage))


def parse_citation(ref: str, docs: dict[str, set[int]]) -> DocVersion:
    doc_id, _, ver = ref.partition("@v")
    if doc_id not in docs:
        raise ValueError(f"unknown document {doc_id!r}")
    version = int(ver) if ver else max(docs[doc_id])
    if version not in docs[doc_id]:
        raise ValueError(f"{doc_id} has no version {version}")
    return DocVersion(doc_id, version)


def iter_steps(case: dict):
    """Yield (step_id, question, expected) for single- and multi-turn cases."""
    if "turns" in case:
        for i, turn in enumerate(case["turns"], start=1):
            yield f"{case['id']}#{i}", turn["question"], turn["expected"]
    else:
        yield case["id"], case["question"], case["expected"]


def validate(golden: dict) -> list[str]:
    """Return a list of problems (empty means the golden set is consistent)."""
    stage = golden.get("dataset", "stage_a")
    if stage not in STAGE_ROOTS:
        return [f"unknown dataset {stage!r}; expected one of {sorted(STAGE_ROOTS)}"]
    docs, access = load_documents(stage), load_access(stage)
    tables = {
        path.stem: pd.read_csv(path)
        for root in STAGE_ROOTS[stage]
        for path in sorted((root / "tables").glob("*.csv"))
    }
    products = set(tables["products"]["product_name"])
    oracle = Oracle.for_stage(stage)
    problems: list[str] = []
    seen: set[str] = set()

    for case in golden["cases"]:
        cid = case["id"]
        if cid in seen:
            problems.append(f"{cid}: duplicate id")
        seen.add(cid)
        if case["category"] not in CATEGORIES:
            problems.append(f"{cid}: unknown category {case['category']!r}")
        persona = case["persona"]
        for table in case.get("tables", []):  # the datasets a correct plan must use
            if table not in tables:
                problems.append(f"{cid}: unknown table {table!r}")
            elif persona not in access.get(table, set()):
                problems.append(f"{cid}: {persona} can't read table {table!r}")

        for step, _question, exp in iter_steps(case):
            if exp["answer"] not in ANSWER_TYPES:
                problems.append(f"{step}: unknown answer type {exp['answer']!r}")
            for ref in exp.get("must_cite", []):
                try:
                    doc = parse_citation(ref, docs)
                except ValueError as e:
                    problems.append(f"{step}: {e}")
                    continue
                if persona not in access.get(doc.doc_id, set()):
                    problems.append(f"{step}: {persona} must cite {ref} but has no access")
            for ref in exp.get("must_not_cite", []):
                try:
                    doc = parse_citation(ref, docs)
                except ValueError as e:
                    problems.append(f"{step}: {e}")
                    continue
                if exp["answer"] == "restricted" and persona in access.get(doc.doc_id, set()):
                    problems.append(f"{step}: 'restricted' but {persona} can read {ref}")
            for option in exp.get("clarify_options", []):
                if option not in products:
                    problems.append(f"{step}: clarify option {option!r} is not a product")
            for phrase, target in exp.get("grounding", {}).items():
                if "column" in target:
                    table, column = target["column"].split(".")
                    if target["value"] not in set(tables[table][column]):
                        problems.append(f"{step}: grounding {phrase!r} -> missing value")
            for spec in (s for item in exp.get("data", []) for s in alternatives(item)):
                try:
                    rows = oracle.run(Query(**spec))
                except (TypeError, ValueError, KeyError) as e:
                    problems.append(f"{step}: bad data query {spec}: {e}")
                    continue
                if exp.get("grounding") and not rows:
                    problems.append(f"{step}: grounded query {spec} matches no rows")
            for metric in exp.get("must_not_use", []):
                if metric not in METRICS:
                    problems.append(f"{step}: must_not_use names unknown metric {metric!r}")
            if exp["answer"] in {"answer"} and not (exp.get("data") or exp.get("must_cite")):
                problems.append(f"{step}: 'answer' case needs data or citations")
    return problems


def alternatives(item: dict) -> list[dict]:
    """A data item is one query, or {"any_of": [...]} when several queries are equally right
    (no-shows counted as shifts or as absences with reason no_show)."""
    return item.get("any_of", [item])


def build_expected(golden: dict, oracle: Oracle) -> dict:
    """step_id -> list of oracle results, one per data item, in order."""

    def result(spec: dict) -> dict:
        return {"query": spec, "rows": oracle.run(Query(**spec))}

    expected: dict[str, list] = {}
    for case in golden["cases"]:
        for step, _question, exp in iter_steps(case):
            if exp.get("data"):
                expected[step] = [
                    {"any_of": [result(s) for s in item["any_of"]]}
                    if "any_of" in item
                    else result(item)
                    for item in exp["data"]
                ]
    return expected


def oracle_for(golden: dict) -> Oracle:
    return Oracle.for_stage(golden.get("dataset", "stage_a"))


def load_golden(name: str = "stage_a") -> dict:
    return yaml.safe_load((GOLDEN_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def render_expected(expected: dict) -> str:
    return json.dumps(expected, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
