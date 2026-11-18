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

from evals.oracle import DEFAULT_DATA, Oracle, Query

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "data" / "reference"
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
}
ANSWER_TYPES = {"answer", "clarify", "abstain", "restricted"}


@dataclass(frozen=True)
class DocVersion:
    doc_id: str
    version: int


def load_documents(doc_dir: Path = REFERENCE / "documents") -> dict[str, set[int]]:
    """doc_id -> set of versions, read from each document's front matter."""
    docs: dict[str, set[int]] = {}
    for path in sorted(doc_dir.glob("*.md")):
        front = path.read_text(encoding="utf-8").split("---")[1]
        meta = yaml.safe_load(front)
        docs.setdefault(meta["doc_id"], set()).add(int(meta["version"]))
    return docs


def load_access(path: Path = REFERENCE / "access.yaml") -> dict[str, set[str]]:
    """source -> groups allowed to read it."""
    return {k: set(v) for k, v in yaml.safe_load(path.read_text())["grants"].items()}


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


def validate(golden: dict, data_dir: Path = DEFAULT_DATA) -> list[str]:
    """Return a list of problems (empty means the golden set is consistent)."""
    docs, access = load_documents(), load_access()
    products = set(pd.read_csv(data_dir / "products.csv")["product_name"])
    tables = {name: pd.read_csv(data_dir / f"{name}.csv") for name in ("stores", "products")}
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
            for spec in exp.get("data", []):
                try:
                    Query(**spec)
                except (TypeError, ValueError) as e:
                    problems.append(f"{step}: bad data query {spec}: {e}")
            if exp["answer"] in {"answer"} and not (exp.get("data") or exp.get("must_cite")):
                problems.append(f"{step}: 'answer' case needs data or citations")
    return problems


def build_expected(golden: dict, oracle: Oracle) -> dict:
    """step_id -> list of oracle results, one per data query, in order."""
    expected: dict[str, list] = {}
    for case in golden["cases"]:
        for step, _question, exp in iter_steps(case):
            if exp.get("data"):
                expected[step] = [
                    {"query": spec, "rows": oracle.run(Query(**spec))} for spec in exp["data"]
                ]
    return expected


def load_golden(name: str = "stage_a") -> dict:
    return yaml.safe_load((GOLDEN_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def render_expected(expected: dict) -> str:
    return json.dumps(expected, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
