"""Check an answer before anyone sees it.

1. Every number in the answer must appear in the evidence (allowing for display rounding),
   in the question itself, or be a year. Anything else is treated as invented.
2. Every citation ([D1]) must refer to real evidence, and answers with evidence must cite it.

Percentages are accepted for ratio metrics (0.0526 may be written 5.26%) and for the
code-computed percentage changes. A sign may be dropped when the text says "down" or "lower";
we accept the absolute value for that reason.
"""

import re
from dataclasses import dataclass, field

from app.answer.evidence import EvidenceItem

CITATION = re.compile(r"\[(D\d+)\]")
# Models often write typographic characters: non-breaking/figure/en/em dashes and minus signs,
# and narrow or non-breaking spaces. Normalise them so dates and negatives are recognised.
DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), "-")
SPACES = dict.fromkeys(map(ord, "\u00a0\u202f\u2009\u2007"), " ")
LIST_MARKER = re.compile(r"^\s*\d{1,2}[.)]\s", re.MULTILINE)  # "1. " or "2) " at a line start
ISO_DATE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b")
NUMBER = re.compile(r"(?<![\w.])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
TOLERANCE = 0.006


@dataclass
class Verification:
    ok: bool
    unsupported_numbers: list[str] = field(default_factory=list)
    unknown_citations: list[str] = field(default_factory=list)
    missing_citation: bool = False

    def feedback(self) -> str:
        problems = []
        if self.unsupported_numbers:
            problems.append(
                "these numbers are not in the evidence: " + ", ".join(self.unsupported_numbers)
            )
        if self.unknown_citations:
            problems.append("these citations don't exist: " + ", ".join(self.unknown_citations))
        if self.missing_citation:
            problems.append("cite the evidence you used, e.g. [D1]")
        return "; ".join(problems)


def _variants(value: float, is_ratio: bool) -> set[float]:
    out = set()
    for v in (value, abs(value)):
        out |= {round(v, 0), round(v, 1), round(v, 2), v}
        if is_ratio:
            out |= {round(v * 100, d) for d in (0, 1, 2)}
    return out


def allowed_numbers(evidence: list[EvidenceItem], question: str) -> set[float]:
    allowed: set[float] = set()
    for item in evidence:
        is_ratio = item.unit == "ratio"
        for row in item.rows:
            for value in row.values():
                if isinstance(value, int | float) and not isinstance(value, bool):
                    allowed |= _variants(float(value), is_ratio and value == row.get(item.metric))
                elif isinstance(value, str):  # period labels like 2026-03 contribute 2026 and 3
                    allowed |= {float(n) for n in re.findall(r"\d+", value)}
        for comp in item.comparisons:
            for key in ("from_value", "to_value", "change", "change_pct"):
                if isinstance(comp.get(key), int | float):
                    allowed |= _variants(float(comp[key]), False)
        allowed.add(float(len(item.rows)))  # "4 countries"
    allowed |= {float(n.replace(",", "")) for n in NUMBER.findall(question)}
    return allowed


def verify(answer: str, evidence: list[EvidenceItem], question: str) -> Verification:
    known_ids = {e.id for e in evidence}
    cited = set(CITATION.findall(answer))
    unknown = sorted(cited - known_ids)
    missing = bool(evidence) and not (cited & known_ids)

    text = answer.translate(DASHES).translate(SPACES)
    text = CITATION.sub(" ", text)
    text = ISO_DATE.sub(" ", text)
    text = LIST_MARKER.sub(" ", text)
    allowed = allowed_numbers(evidence, question)
    unsupported = []
    for raw in NUMBER.findall(text):
        value = float(raw.replace(",", "").replace("−", "-"))
        if 1900 <= value <= 2100 and value.is_integer():
            continue  # years
        if not any(abs(abs(value) - abs(a)) <= TOLERANCE for a in allowed):
            unsupported.append(raw)
    return Verification(
        ok=not unsupported and not unknown and not missing,
        unsupported_numbers=unsupported,
        unknown_citations=unknown,
        missing_citation=missing,
    )
