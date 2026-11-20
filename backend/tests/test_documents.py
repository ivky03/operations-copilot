"""Document chunking, indexing on publish, and permission-filtered hybrid search.

Uses the real local embedding model (deterministic, no network after the first download), so
these tests exercise the same search users get.
"""

import pytest
from sqlalchemy import func, select

from app.cli import publish_reference, seed_reference
from app.documents.chunking import MAX_WORDS, chunk_markdown
from app.documents.index import reindex_all
from app.documents.search import _keyword_query, search
from app.ingest.publish import publish_document
from app.models import DocChunk, Source, SourceStatus, SourceVersion, User
from evals.golden import REFERENCE, load_access
from evals.retrieval_eval import (
    PERSONA_GROUPS,
    Result,
    load_retrieval_golden,
    score_query,
    summarise,
    validate,
)

pytestmark = pytest.mark.integration

OPS = "ops.employee@brindlewood.example"
FINANCE = "finance.employee@brindlewood.example"


# ----------------------------------------------------------------------------- chunking


def test_chunks_follow_headings_and_keep_the_introduction():
    body = "# Policy\n\nIntro text.\n\n## Scope\n\nApplies to all.\n\n## Rules\n\nBe kind.\n"
    title, chunks = chunk_markdown(body)
    assert title == "Policy"
    assert [(c.ordinal, c.heading, c.content) for c in chunks] == [
        (0, "Introduction", "Intro text."),
        (1, "Scope", "Applies to all."),
        (2, "Rules", "Be kind."),
    ]
    assert chunks[1].embedding_text(title) == "Policy: Scope\nApplies to all."


def test_subsections_and_long_sections_are_split_with_their_heading():
    para = " ".join(["word"] * (MAX_WORDS // 2 + 10))
    body = f"# T\n\n## A\n\nshort\n\n### B\n\n{para}\n\n{para}\n"
    _, chunks = chunk_markdown(body)
    assert [c.heading for c in chunks] == ["A", "A › B (part 1)", "A › B (part 2)"]
    assert all(len(c.content.split()) <= MAX_WORDS for c in chunks)


def test_tables_and_lists_stay_inside_their_section():
    body = "# T\n\n## Times\n\n| A | B |\n|---|---|\n| x | 1 |\n\n- one\n- two\n"
    _, chunks = chunk_markdown(body)
    assert len(chunks) == 1 and "| x | 1 |" in chunks[0].content and "- two" in chunks[0].content


# ----------------------------------------------------------------------------- indexing


@pytest.fixture
def reference(db):
    seed_reference(db)
    publish_reference(db)
    return {
        "ops": db.scalar(select(User).where(User.email == OPS)),
        "finance": db.scalar(select(User).where(User.email == FINANCE)),
    }


def _chunk_count(db, slug: str, version: int) -> int:
    return db.scalar(
        select(func.count())
        .select_from(DocChunk)
        .join(SourceVersion, SourceVersion.id == DocChunk.source_version_id)
        .join(Source, Source.id == SourceVersion.source_id)
        .where(Source.slug == slug, SourceVersion.version == version)
    )


def test_publishing_a_document_indexes_its_sections(db, reference):
    assert _chunk_count(db, "returns-refund-policy", 1) == 4  # kept for history questions
    assert _chunk_count(db, "returns-refund-policy", 2) == 7
    model = db.scalar(select(DocChunk.embedding_model).limit(1))
    assert model == "BAAI/bge-small-en-v1.5"


def test_reindexing_replaces_chunks_instead_of_duplicating(db, reference):
    before = db.scalar(select(func.count()).select_from(DocChunk))
    reindex_all(db)
    reindex_all(db)
    assert db.scalar(select(func.count()).select_from(DocChunk)) == before


def test_republishing_identical_content_changes_nothing(db, reference):
    path = REFERENCE / "documents" / "cancellation-policy.v1.md"
    before = db.scalar(select(func.count()).select_from(DocChunk))
    assert publish_document(db, path.read_bytes()).status == "unchanged"
    assert db.scalar(select(func.count()).select_from(DocChunk)) == before


def test_full_text_column_is_generated_by_the_database(db, reference):
    chunk = db.scalar(select(DocChunk).where(DocChunk.heading == "Return window").limit(1))
    assert "window" in str(chunk.search_text)


# ----------------------------------------------------------------------------- search


def test_search_finds_the_section_that_answers(db, reference):
    hits = search(db, reference["ops"], "What is our return window?", k=3)
    assert hits[0].label == "returns-refund-policy@v2"
    assert hits[0].heading == "Return window"
    assert hits[0].current and hits[0].effective_date == "2026-03-01"
    assert set(hits[0].ranks) == {"keyword", "vector"}


def test_exact_codes_are_found_by_keyword_search(db, reference):
    hits = search(db, reference["ops"], "DEC-5001", k=3, mode="keyword")
    assert hits and hits[0].heading == "Range changes"


def test_superseded_versions_only_when_history_is_asked_for(db, reference):
    q = "What was the return window before March 2026?"
    current = search(db, reference["ops"], q, k=10)
    history = search(db, reference["ops"], q, k=10, include_superseded=True)
    assert all(h.current for h in current)
    old = [h for h in history if h.label == "returns-refund-policy@v1"]
    assert old and not old[0].current


@pytest.mark.parametrize(
    "persona, question, forbidden",
    [
        ("ops", "What are our gross margin targets by category?", "finance-margin-targets"),
        ("ops", "Lighting 55% Kitchen 48% target margin confidential", "finance-margin-targets"),
        ("ops", "month-end close timeline sign-off", "month-end-close"),
        ("finance", "What are the delivery charges for web orders?", "shipping-and-delivery"),
    ],
)
def test_restricted_documents_are_never_returned(db, reference, persona, question, forbidden):
    for mode in ("keyword", "vector", "hybrid"):
        hits = search(db, reference[persona], question, k=50, mode=mode)
        assert hits, "search should still return passages the user may read"
        assert forbidden not in {h.source for h in hits}


def test_archived_documents_are_not_searched(db, reference):
    source = db.scalar(select(Source).where(Source.slug == "market-launches"))
    source.status = SourceStatus.ARCHIVED
    db.commit()
    hits = search(db, reference["ops"], "When did the French web shop launch?", k=50)
    assert "market-launches" not in {h.source for h in hits}


def test_inactive_user_gets_nothing(db, reference):
    reference["ops"].is_active = False
    db.commit()
    assert search(db, reference["ops"], "return window") == []


@pytest.mark.parametrize("text", ["':* & | ! ( )", "return & window | !refund", "<->", ""])
def test_keyword_query_cannot_be_broken_by_operators(db, reference, text):
    search(db, reference["ops"], text, mode="keyword")  # must not raise


def test_keyword_query_ors_plain_words():
    assert _keyword_query("Return-window: 30 days?!") == "return | window | 30 | days"
    assert _keyword_query("!!!") is None


# ----------------------------------------------------------------------------- evaluation


def test_retrieval_labels_are_valid():
    assert validate(load_retrieval_golden()) == []


def test_eval_scoring_counts_leaks_stale_and_ranks():
    access = {"a": {"operations"}, "secret": {"finance"}}
    query = {"id": "q", "persona": "operations", "relevant": ["a@v2#X"]}
    results = [
        Result("secret", 1, "S", True),  # leak
        Result("a", 1, "X", False),  # stale (old version, no history asked)
        Result("a", 2, "X", True),  # relevant, rank 3
    ]
    score = score_query(query, results, access)
    assert score.leaks == ["secret@v1#S"] and score.stale == ["a@v1#X"]
    assert score.first_rank == 3
    summary = summarise([score])
    assert summary["hit@1"] == 0 and summary["hit@3"] == 1 and summary["mrr"] == 0.333


def test_retrieval_quality_does_not_regress(db, reference):
    """Thresholds sit just below the first measured results (hybrid: hit@5 1.0, MRR 0.914 on
    2026-11-19). A change that makes search worse fails here, in CI, before it merges."""
    access = load_access()
    golden = load_retrieval_golden()
    scores = []
    for q in golden["queries"]:
        hits = search(
            db,
            reference["ops" if q["persona"] == "operations" else "finance"],
            q["query"],
            k=5,
            include_superseded=bool(q.get("history")),
        )
        results = [Result(h.source, h.version, h.heading, h.current) for h in hits]
        scores.append(score_query(q, results, access))
    summary = summarise(scores)
    assert summary["leaks"] == 0
    assert summary["stale"] == 0
    assert summary["hit@5"] >= 0.95
    assert summary["mrr"] >= 0.85
    assert set(PERSONA_GROUPS) == {"operations", "finance"}
