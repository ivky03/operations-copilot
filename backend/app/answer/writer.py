"""Writer: one model call that turns computed evidence into a short answer, with citations.

The model may only use numbers present in the evidence (including the code-computed
comparisons). verify.py enforces this; if verification fails twice, `template_answer`
renders the evidence as plain tables instead, so an unsupported number is never shown.
"""

import json

from pydantic import BaseModel, ConfigDict

from app.answer.evidence import EvidenceItem
from app.llm.client import CallRecord, LLMClient

PROMPT_VERSION = "writer-v2"


class WrittenAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


SYSTEM = """You write answers for Operations Copilot from computed evidence.
Reply with ONE JSON object: {"answer": "<markdown text>"}.

Rules:
- Use ONLY numbers that appear in the evidence (rows or comparisons). Never calculate new
  numbers yourself: no sums, differences, averages or percentages that aren't given.
- EVERY sentence or bullet that contains a number must end with its citation, like [D1].
  Answers without citations are rejected.
- Use bullet points, not numbered lists. Write dates exactly as they appear in the evidence.
- Money is in EUR: write it like €7,070.00. Ratios may be written as percentages (0.0526 → 5.26%).
- An empty result means there was no matching data: say so plainly; don't call it an error.
- If the evidence notes say a phrase was interpreted (e.g. "UK" → "United Kingdom"), mention it.
- Each evidence item includes the metric's official definition. Use it to explain results:
  e.g. if a metric is built from parts counted in the period each happened, a value can be
  negative, which is not an error.
- Be concise: a direct answer first, then a short list or table if helpful. No speculation
  about causes the evidence doesn't show."""


def _evidence_payload(evidence: list[EvidenceItem]) -> str:
    items = []
    for e in evidence:
        item = {
            "id": e.id,
            "metric": e.metric,
            "title": e.title,
            "definition": e.definition,
            "unit": e.unit,
            "query": e.description,
            "rows": e.rows[:50],
        }
        if len(e.rows) > 50:
            item["note"] = f"{len(e.rows)} rows; first 50 shown"
        if e.comparisons:
            item["comparisons"] = e.comparisons
        items.append(item)
    return json.dumps(items, ensure_ascii=False, default=str)


def write_answer(
    llm: LLMClient,
    question: str,
    evidence: list[EvidenceItem],
    notes: list[str],
    calls: list[CallRecord],
    feedback: str | None = None,
) -> str:
    content = f"# Question\n{question}\n\n# Evidence\n{_evidence_payload(evidence)}"
    if notes:
        content += "\n\n# Notes\n" + "\n".join(f"- {n}" for n in notes)
    if feedback:
        content += (
            f"\n\n# Your previous answer was rejected\n{feedback}\n"
            "Write it again using only numbers from the evidence."
        )
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]
    purpose = "answer_retry" if feedback else "answer"
    return llm.complete_json(purpose, messages, WrittenAnswer, calls).answer


def _fmt(value: object, unit: str | None, is_metric: bool) -> str:
    if value is None:
        return "–"
    if isinstance(value, float) and is_metric:
        if unit == "EUR":
            return f"-€{abs(value):,.2f}" if value < 0 else f"€{value:,.2f}"
        if unit == "ratio":
            return f"{value:.2%}"
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def template_answer(evidence: list[EvidenceItem], notes: list[str]) -> str:
    """Deterministic fallback: the evidence as tables. Used when the writer can't be verified."""
    parts = [
        "Here are the computed results (shown as tables because a written summary "
        "couldn't be verified):"
    ]
    parts += [f"_{n}_" for n in notes]
    for e in evidence:
        parts.append(f"\n**{e.title}**: {e.description} [{e.id}]")
        if not e.rows:
            parts.append("No matching data.")
            continue
        parts.append("| " + " | ".join(e.columns) + " |")
        parts.append("|" + "---|" * len(e.columns))
        for row in e.rows:
            cells = [_fmt(row[c], e.unit, c == e.metric) for c in e.columns]
            parts.append("| " + " | ".join(cells) + " |")
    return "\n".join(parts)
