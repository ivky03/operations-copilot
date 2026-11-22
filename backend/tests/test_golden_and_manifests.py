"""The golden set, documents, access grants and manifests are consistent with each other."""

import copy
import csv

import pytest
import yaml

from evals.golden import (
    GOLDEN_DIR,
    REFERENCE,
    build_expected,
    load_access,
    load_documents,
    load_golden,
    oracle_for,
    render_expected,
    validate,
)

MANIFESTS = sorted((REFERENCE / "manifests").glob("*.yaml"))


@pytest.fixture(scope="module")
def golden():
    return load_golden("stage_a")


def test_golden_set_is_valid(golden):
    assert validate(golden) == []


def test_held_out_set_is_valid():
    held_out = load_golden("stage_a.heldout")
    assert validate(held_out) == []
    tuning_ids = {c["id"] for c in load_golden("stage_a")["cases"]}
    assert not tuning_ids & {c["id"] for c in held_out["cases"]}


def test_stage_b_set_is_valid_and_stage_aware():
    stage_b = load_golden("stage_b")
    assert stage_b["dataset"] == "stage_b"
    assert validate(stage_b) == []
    # Documents and grants come from the set's own stage: Stage A has no absence policy.
    assert "absence-policy" in load_documents("stage_b")
    assert "absence-policy" not in load_documents("stage_a")
    assert load_access("stage_b")["staff"] == {"operations"}


@pytest.mark.parametrize("name", ["stage_a", "stage_b"])
def test_committed_expected_answers_are_current(name):
    """Drift guard: data or golden changes must be followed by `python -m evals.build_expected`."""
    golden = load_golden(name)
    fresh = render_expected(build_expected(golden, oracle_for(golden)))
    committed = (GOLDEN_DIR / f"{name}.expected.json").read_text(encoding="utf-8")
    assert fresh == committed, (
        f"{name}.expected.json is stale: run `uv run python -m evals.build_expected {name}`"
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        # Finance may not read the absence policy (Operations only)
        (lambda c: c["expected"].update(must_cite=["absence-policy"]), "has no access"),
        (lambda c: c["expected"].update(must_not_use=["profit"]), "unknown metric"),
        # "category" on purchase orders could be the product's or the supplier's
        (
            lambda c: c["expected"].update(
                data=[{"metric": "units_ordered", "group_by": ["category"]}]
            ),
            "bad data query",
        ),
        (
            lambda c: c["expected"].update(data=[{"metric": "headcount", "group_by": ["vibe"]}]),
            "bad data query",
        ),
    ],
)
def test_validator_catches_mistakes_in_the_stage_b_set(mutate, message):
    broken = copy.deepcopy(load_golden("stage_b"))
    case = next(c for c in broken["cases"] if c["id"] == "pol-b03")  # a finance persona
    mutate(case)
    assert any(message in p for p in validate(broken)), validate(broken)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c["expected"].update(must_cite=["finance-margin-targets"]), "has no access"),
        (lambda c: c["expected"].update(must_cite=["no-such-doc"]), "unknown document"),
        (lambda c: c["expected"].update(must_cite=["returns-refund-policy@v9"]), "no version 9"),
        (lambda c: c["expected"].update(clarify_options=["Purple Lamp"]), "is not a product"),
        (lambda c: c["expected"].update(data=[{"metric": "profit"}]), "bad data query"),
        (lambda c: c.update(category="vibes"), "unknown category"),
    ],
)
def test_validator_catches_mistakes_in_the_golden_set(golden, mutate, message):
    """The validator must actually fail on bad cases, not just pass on good ones."""
    broken = copy.deepcopy(golden)
    case = next(c for c in broken["cases"] if c["id"] == "pol-001")  # an operations persona
    mutate(case)
    problems = validate(broken)
    assert any(message in p for p in problems), problems


def test_every_document_is_labeled_synthetic_and_granted():
    access = load_access()
    for path in sorted((REFERENCE / "documents").glob("*.md")):
        meta = yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])
        assert meta.get("synthetic") is True, f"{path.name} must be labeled synthetic"
        for key in ("doc_id", "version", "title", "owner", "effective_date"):
            assert key in meta, f"{path.name} is missing {key}"
        # No implicit access: a source without a grant would be readable by nobody (or worse).
        assert meta["doc_id"] in access, f"{meta['doc_id']} has no access grant"


def test_access_grants_refer_to_real_sources_and_groups():
    access = load_access()
    groups = set(yaml.safe_load((REFERENCE / "access.yaml").read_text())["groups"])
    sources = set(load_documents()) | {m.stem for m in MANIFESTS}
    assert set(access) == sources, "grants and sources must match exactly"
    for source, allowed in access.items():
        assert allowed <= groups, f"{source} grants an unknown group"


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda p: p.stem)
def test_manifest_matches_its_csv(manifest_path):
    m = yaml.safe_load(manifest_path.read_text())
    with (REFERENCE / "tables" / f"{m['source']}.csv").open() as f:
        header = next(csv.reader(f))
    columns = [c["name"] for c in m["columns"]]
    assert columns == header, "manifest columns must match the CSV header, in order"
    assert set(m["primary_key"]) <= set(columns)
    for rel in m.get("relationships", []):
        assert rel["column"] in columns
        target_table, target_col = rel["references"].split(".")
        target = yaml.safe_load((REFERENCE / "manifests" / f"{target_table}.yaml").read_text())
        assert target_col in [c["name"] for c in target["columns"]]
    names = [x["name"] for x in m.get("metrics", []) + m.get("derived_metrics", [])]
    assert len(names) == len(set(names)), "metric names must be unique"
    for metric in m.get("metrics", []):
        for f in metric.get("filters", []):
            assert f["column"] in columns
