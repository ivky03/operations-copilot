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


def _passage(pid, source, version=1):
    return {"id": pid, "source": source, "version": version}


def test_restricted_fails_if_a_forbidden_passage_was_even_retrieved():
    exp = {"answer": "restricted", "must_not_cite": ["finance-margin-targets"]}
    clean = {"status": "unanswerable", "evidence": [], "passages": [_passage("C1", "x")]}
    assert score_case("p", "permission", exp, clean, None).passed
    leaked = {**clean, "passages": [_passage("C1", "finance-margin-targets")], "answer": "no"}
    s = score_case("p", "permission", exp, leaked, None)
    assert not s.passed and "finance-margin-targets@v1" in s.notes[0]
    assert not score_case("p", "permission", exp, {**clean, "status": "error"}, None).passed


def test_policy_answers_must_cite_the_right_document_version():
    exp = {
        "answer": "answer",
        "must_cite": ["returns-refund-policy@v2"],
        "must_not_cite": ["returns-refund-policy@v1"],
    }
    passages = [
        _passage("C1", "returns-refund-policy", 2),
        _passage("C2", "returns-refund-policy", 1),
    ]
    base = {"status": "answered", "evidence": [], "passages": passages}
    assert score_case("pol", "policy", exp, {**base, "answer": "30 days [C1]."}, None).passed
    s = score_case("pol", "policy", exp, {**base, "answer": "30 days [C1, C2]."}, None)
    assert not s.passed and s.citations_ok is False and "must not" in s.notes[0]
    s = score_case("pol", "policy", exp, {**base, "answer": "30 days."}, None)
    assert not s.passed and "did not cite returns-refund-policy@v2" in s.notes


def test_unversioned_must_cite_accepts_any_version_but_only_if_cited():
    exp = {"answer": "answer", "must_cite": ["cancellation-policy"]}
    base = {
        "status": "answered",
        "evidence": [],
        "passages": [_passage("C1", "cancellation-policy")],
    }
    assert score_case("p", "policy", exp, {**base, "answer": "See [C1]"}, None).passed
    assert not score_case(
        "p", "policy", exp, {**base, "answer": "Retrieved, not cited"}, None
    ).passed


def test_follow_ups_are_not_scored_until_conversations_exist():
    s = score_case("fu-1", "follow_up", {"answer": "answer"}, {"status": "x", "evidence": []}, None)
    assert s.scored is False and s.passed is None and "M5" in s.notes[0]


def test_percentile():
    assert percentile([100, 200, 300, 400, 1000], 50) == 300
    assert percentile([], 50) is None
