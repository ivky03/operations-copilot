"""Value grounding: map a phrase from a question to values that actually exist in the data.

Order of preference:
1. Exact match (case-insensitive) on a real value or a manifest alias: "UK" → United Kingdom.
2. Fuzzy match with trigram similarity: "untied kingdom" → United Kingdom,
   "berlin shop" → Berlin Store.
3. If several candidates score about the same, it's ambiguous: "desk lamp" matches Blue Desk
   Lamp, White Desk Lamp and Desk Lamp Shade, so we ask which one instead of guessing.

Thresholds were chosen from scores measured on the reference data:
clear matches scored 0.60-1.00 with the runner-up at least 0.25 lower, while genuinely
ambiguous phrases had several candidates within 0.07 of each other.
Only the user's readable, published dataset versions are searched.
"""

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.query.catalog import Catalog

MIN_SCORE = 0.5  # below this, it isn't a match
TIE_MARGIN = 0.15  # candidates within this of the best are treated as equally likely

Status = Literal["exact", "alias", "fuzzy", "ambiguous", "not_found"]


@dataclass(frozen=True)
class Candidate:
    dataset: str
    column: str
    value: str  # canonical value as stored in the data
    score: float


@dataclass
class Grounding:
    phrase: str
    status: Status
    match: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)


_SCORE = (
    "greatest(similarity(v.value_norm, :p), word_similarity(:p, v.value_norm), "
    "word_similarity(v.value_norm, :p))"
)


def ground(
    db: Session,
    catalog: Catalog,
    phrase: str,
    column: tuple[str, str] | None = None,  # (dataset slug, column) to restrict the search
) -> Grounding:
    phrase_norm = " ".join(phrase.lower().split())
    if not phrase_norm:
        return Grounding(phrase, "not_found")
    scope = {
        ds.version_id: ds.slug
        for ds in catalog.datasets.values()
        if column is None or ds.slug == column[0]
    }
    if not scope:
        return Grounding(phrase, "not_found")
    params: dict[str, object] = {"p": phrase_norm, "versions": list(scope)}
    column_filter = ""
    if column is not None:
        column_filter = "AND v.column_name = :col"
        params["col"] = column[1]

    # 1. Exact (value or alias), case-insensitive.
    # Reviewed for injection: the only formatted parts are fixed strings defined in this module;
    # the phrase, versions and column are bound parameters.
    exact = db.execute(
        text(
            "SELECT DISTINCT v.source_version_id::text, v.column_name, v.canonical, v.is_alias "  # noqa: S608
            "FROM value_index v WHERE v.value_norm = :p "
            f"AND v.source_version_id::text = ANY(:versions) {column_filter}"
        ),
        params,
    ).all()
    if exact:
        found = {(scope[r[0]], r[1], r[2]) for r in exact}
        cands = [Candidate(ds, col, val, 1.0) for ds, col, val in sorted(found)]
        if len(cands) == 1:
            return Grounding(phrase, "alias" if exact[0][3] else "exact", cands[0], cands)
        return Grounding(phrase, "ambiguous", None, cands)

    # 2. Fuzzy: best score per canonical value.
    rows = db.execute(
        text(  # fixed strings only; values are bound parameters
            f"SELECT v.source_version_id::text, v.column_name, v.canonical, max({_SCORE}) AS score "  # noqa: S608
            "FROM value_index v "
            f"WHERE v.source_version_id::text = ANY(:versions) {column_filter} "
            "GROUP BY 1, 2, 3 ORDER BY score DESC, 3 LIMIT 10"
        ),
        params,
    ).all()
    cands = [Candidate(scope[r[0]], r[1], r[2], round(float(r[3]), 2)) for r in rows]
    cands = [c for c in cands if c.score >= MIN_SCORE]
    if not cands:
        return Grounding(phrase, "not_found")
    close = [c for c in cands if c.score >= cands[0].score - TIE_MARGIN]
    if len(close) == 1:
        return Grounding(phrase, "fuzzy", close[0], close)
    return Grounding(phrase, "ambiguous", None, close)
