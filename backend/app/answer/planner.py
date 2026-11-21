"""Planner: one model call that turns a question into a validated AnswerPlan.

The model sees the question, the schema context (never data rows) and the titles of the
documents this user can read, and must reply with JSON matching AnswerPlan. Filter values
are copied from the user's words; grounding maps them to real values afterwards
("UK" → "United Kingdom"), so the model doesn't need to know them all.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.answer.context import SchemaContext
from app.llm.client import CallRecord, LLMClient
from app.query.plan import QueryPlan

PROMPT_VERSION = "planner-v6"


class AnswerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["data", "clarify", "unanswerable", "documents"]
    standalone_question: str | None = None  # the question made complete without the chat
    queries: list[QueryPlan] = Field(default=[], max_length=3)
    doc_searches: list[str] = Field(default=[], max_length=3)  # short document search phrases
    include_history: bool = False  # also search superseded document versions
    clarification: str | None = None  # question to ask the user when kind = clarify
    reason: str | None = None  # why, when kind = unanswerable


SYSTEM = """You plan answers for Operations Copilot, an assistant over a company's governed data.
You never answer the question yourself. You reply with ONE JSON object that describes how to
answer it, using only the metrics and columns listed in the data description and the
documents listed under "Documents".

JSON fields:
- "standalone_question": the question rewritten so it is complete without the conversation:
    resolve "that", "it", "they", "and April?", "break that down by X", "before that", and
    replies to a question we asked (e.g. "the blue one"). Keep the user's own words for
    values. Without a conversation, repeat the question. Plan THIS question.
- "kind": one of
    "data"          the question can be answered with the listed metrics
    "clarify"       the question is too vague (no metric, or no idea what period/scope is meant)
    "documents"     it asks about policies, procedures, definitions, dates of events or
                    explanations that the listed documents may cover, and needs no numbers
                    from the data
    "unanswerable"  neither a listed metric nor a listed document could cover it (e.g. web
                    traffic or customer satisfaction when nothing listed mentions them), or
                    it asks for a period outside the data's date range
- "queries": for kind "data", 1 to 3 query objects (one per metric needed). Each query:
    "metric":      a metric name exactly as listed
    "dimensions":  column names to group by (e.g. ["region"]); [] for a single total
    "filters":     [{"dimension": "<column>", "op": "=" | "in" | "!=" | "not in",
                     "values": ["<words from the question>"]}]
                   Copy the user's EXACT words for values (e.g. "UK", "north"). Never
                   replace them with a value from the examples list, even if one looks close:
                   matching to real values, and asking the user when several match, happens
                   later. Use "in" for several values.
    "time_range":  {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} with end EXCLUSIVE
                   (March 2026 is start 2026-03-01, end 2026-04-01). Omit for all time.
    "time_grain":  "month" (or day/week/quarter/year) to break results down over time
    "order":       "dimensions" (default), "metric_desc" (largest first) or "metric_asc"
    "limit":       an integer for top-N questions
- "doc_searches": 1 to 3 short search phrases for the documents (e.g. ["return window"],
    ["cancellation handling", "net sales definition"]). Required for kind "documents".
    Also add them to kind "data" when the question asks for a policy, rule, definition,
    "how is it calculated" or a "why" explanation, so the answer can cite the documents.
- "include_history": true ONLY when the question asks about a previous, old or superseded
    policy, or what applied before a date. Otherwise omit it.
- "clarification": for kind "clarify", one short question to ask the user.
- "reason": for kind "unanswerable", one short sentence.

Rules:
- Use "time_grain" ONLY when the question asks for a breakdown over time ("by month",
  "monthly", "each month", "trend", "over time") or compares periods. A period mentioned only
  as a filter ("in Q1", "between January and April", "in March") means ONE total for that
  period: set "time_range" and omit "time_grain".
- Comparing periods (e.g. February vs March): ONE query covering both periods with
  "time_grain": "month" and the dimensions requested.
- "Why" questions about a derived metric (one with a "Formula" in the data description): also
  query each metric named in its formula, with the same filters, so the answer can show what
  drove the result.
- "Q1" means January to March. "Last month" and "this month" are relative to the end of the
  data's date range, given below.
- Never invent metric or column names. If no listed metric fits, use "unanswerable".
- If the question mixes numbers with a policy or explanation (e.g. "how much did X total, and
  what is the rule for Y?", "why is there no data for Z in February?"), use kind "data" with
  "queries" for the numeric part AND "doc_searches" for the rest. Never use "documents" or
  "unanswerable" when part of the question can be answered with the listed metrics.
- Before choosing "unanswerable", read the Documents list: targets, budgets, policies,
  procedures and plans are often in a document even when no metric exists. If a listed
  document's title could cover the question, use "documents" (or add "doc_searches").
- Reply with the JSON object only."""


def plan_question(
    llm: LLMClient,
    question: str,
    context: SchemaContext,
    calls: list[CallRecord],
    documents: list[str] | None = None,
    history: str = "",
) -> AnswerPlan:
    data_range = (
        f"The data covers {context.data_start} to {context.data_end}. "
        f"Treat {context.data_end} as today."
        if context.data_end
        else "No dated data is available."
    )
    docs = "\n".join(f"- {t}" for t in documents or []) or "(none)"
    messages = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": f"# Data description\n{context.text}\n\n{data_range}\n\n"
            f"# Documents\n{docs}\n\n"
            + (
                f"# Conversation so far\n{history}\n\n# New question\n"
                if history
                else "# Question\n"
            )
            + question,
        },
    ]
    return llm.complete_json("plan", messages, AnswerPlan, calls)
