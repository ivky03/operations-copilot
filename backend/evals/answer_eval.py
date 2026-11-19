"""Scoring for the answer evaluation (real model). Pure functions, unit-tested without any model.

A case passes when:
- behaviour: the pipeline's status matches what the golden case expects
  (answer → answered, clarify → clarification, abstain → unanswerable,
  restricted → nothing answered from restricted sources), and
- data (when the case declares data): every expected oracle result is matched by one of the
  answer's evidence items, by metric and rows (rounded per metric type).

Evidence may carry an extra column that is constant across its rows (e.g. "month": "2026-02"
when the planner grouped a single-month question by month). Such a column carries no extra
information, so it is dropped before comparing; any other difference is a failure.
"""

from dataclasses import dataclass, field

ANSWERED = {"answered", "answered_fallback"}
EXPECTED_STATUS = {
    "answer": ANSWERED,
    "clarify": {"clarification"},
    "abstain": {"unanswerable"},
}
# Categories needing capabilities that don't exist yet; reported, not scored.
NOT_YET = {
    "policy": "needs document retrieval (M4)",
    "follow_up": "needs conversations (M5)",
}
RATIO_METRICS = {"cancellation_rate"}


@dataclass
class CaseScore:
    case_id: str
    category: str
    scored: bool
    behaviour_ok: bool | None = None
    data_ok: bool | None = None
    verified: bool | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool | None:
        if not self.scored:
            return None
        return bool(self.behaviour_ok) and self.data_ok is not False


def _round(metric: str, value):
    if value is None:
        return None
    return round(float(value), 4 if metric in RATIO_METRICS else 2)


def normalise_rows(rows: list[dict], metric: str, keep: set[str] | None = None) -> list[dict]:
    """Round the metric; if `keep` is given, drop other columns that are constant across rows."""
    if keep is not None and rows:
        extra = set(rows[0]) - keep
        constant = {c for c in extra if len({repr(r.get(c)) for r in rows}) == 1}
        dropped = constant
    else:
        dropped = set()
    out = []
    for row in rows:
        clean = {k: v for k, v in row.items() if k not in dropped}
        if metric in clean:
            clean[metric] = _round(metric, clean[metric])
        out.append(clean)
    return out


def rows_match(expected: list[dict], got: list[dict], metric: str, ordered: bool) -> bool:
    keep = set(expected[0]) if expected else {metric}
    exp = normalise_rows(expected, metric)
    act = normalise_rows(got, metric, keep)
    if ordered:
        return exp == act
    key = lambda r: sorted((k, repr(v)) for k, v in r.items())  # noqa: E731
    return sorted(exp, key=key) == sorted(act, key=key)


def data_matches(expected_items: list[dict], evidence: list[dict]) -> tuple[bool, list[str]]:
    """Every expected oracle result must be matched by some evidence item."""
    notes = []
    for item in expected_items:
        spec, rows = item["query"], item["rows"]
        metric = spec["metric"]
        ordered = bool(spec.get("order_by_value"))
        if not any(
            e["metric"] == metric and rows_match(rows, e["rows"], metric, ordered) for e in evidence
        ):
            got = [(e["metric"], len(e["rows"])) for e in evidence]
            notes.append(f"expected {metric} ({len(rows)} rows) not matched; got {got}")
    return not notes, notes


def score_case(
    case_id: str, category: str, expected: dict, result: dict, oracle: list | None
) -> CaseScore:
    """`result`: {status, answer, evidence: [{metric, rows}]}; `oracle`: expected data items."""
    if category in NOT_YET:
        return CaseScore(case_id, category, scored=False, notes=[NOT_YET[category]])
    score = CaseScore(case_id, category, scored=True)
    status = result["status"]
    want = expected["answer"]
    if want == "restricted":
        # Documents aren't searchable yet, so this only proves nothing was answered.
        score.behaviour_ok = status not in ANSWERED and not result["evidence"]
        score.notes.append("restricted-content check is partial until documents exist (M4)")
    else:
        score.behaviour_ok = status in EXPECTED_STATUS[want]
    if not score.behaviour_ok:
        score.notes.append(f"expected '{want}', got status '{status}'")
    if oracle and want == "answer":
        score.data_ok, notes = data_matches(oracle, result["evidence"])
        score.notes += notes
    if want == "answer" and status in ANSWERED:
        score.verified = status == "answered"
    if category == "combined" and expected.get("must_cite"):
        score.notes.append("document citations not scored until M4")
    return score


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[index]
