"""Unit tests for the evaluation scoring rules (no model involved)."""

from evals.answer_eval import data_matches, percentile, rows_match, score_case

ORACLE = [
    {
        "query": {"metric": "net_sales", "group_by": ["country"]},
        "rows": [
            {"country": "Germany", "net_sales": 7070.0},
            {"country": "Spain", "net_sales": 2396.5},
        ],
    }
]


def ev(rows, metric="net_sales"):
    return {"metric": metric, "rows": rows}


def test_rows_match_ignores_order_and_display_precision():
    got = [
        {"country": "Spain", "net_sales": 2396.5000001},
        {"country": "Germany", "net_sales": 7070},
    ]
    assert rows_match(ORACLE[0]["rows"], got, "net_sales", ordered=False)
    assert not rows_match(ORACLE[0]["rows"], got, "net_sales", ordered=True)


def test_constant_extra_column_is_ignored_but_varying_one_is_not():
    expected = [{"order_count": 12}]
    assert rows_match(expected, [{"month": "2026-02", "order_count": 12}], "order_count", False)
    two_months = [{"month": "2026-01", "order_count": 5}, {"month": "2026-02", "order_count": 7}]
    assert not rows_match(expected, two_months, "order_count", False)


def test_wrong_numbers_fail():
    ok, notes = data_matches(
        ORACLE,
        [
            ev(
                [
                    {"country": "Germany", "net_sales": 7071.0},
                    {"country": "Spain", "net_sales": 2396.5},
                ]
            )
        ],
    )
    assert not ok and "not matched" in notes[0]


def test_behaviour_mapping():
    answered = {"status": "answered", "evidence": [ev(ORACLE[0]["rows"])]}
    assert score_case("c", "calculation", {"answer": "answer"}, answered, ORACLE).passed
    assert not score_case("c", "calculation", {"answer": "clarify"}, answered, None).passed
    assert score_case(
        "a", "ambiguous", {"answer": "clarify"}, {"status": "clarification", "evidence": []}, None
    ).passed
    assert score_case(
        "u", "unanswerable", {"answer": "abstain"}, {"status": "unanswerable", "evidence": []}, None
    ).passed


def test_fallback_answer_passes_but_is_not_counted_as_verified():
    fallback = {"status": "answered_fallback", "evidence": [ev(ORACLE[0]["rows"])]}
    s = score_case("c", "calculation", {"answer": "answer"}, fallback, ORACLE)
    assert s.passed and s.verified is False


def test_restricted_passes_only_if_nothing_was_answered():
    exp = {"answer": "restricted"}
    assert score_case("p", "permission", exp, {"status": "documents", "evidence": []}, None).passed
    assert not score_case(
        "p", "permission", exp, {"status": "answered", "evidence": [ev([])]}, None
    ).passed


def test_categories_needing_future_milestones_are_not_scored():
    s = score_case(
        "pol-1", "policy", {"answer": "answer"}, {"status": "documents", "evidence": []}, None
    )
    assert s.scored is False and s.passed is None and "M4" in s.notes[0]


def test_percentile():
    assert percentile([100, 200, 300, 400, 1000], 50) == 300
    assert percentile([], 50) is None
