"""Answer cache: reuse a saved answer when nothing it could depend on has changed.

An answer depends on the question, on everything the planner is shown (the schema of every
dataset the asker can read, and the titles of their documents), on what search can find, on the
data itself, and on the models and prompts. The key is a hash of all of it:

    normalised question
    + every source the asker can read, with its published version
    + planner and writer models, and the prompt versions
    + how the planner's tables are chosen (full or retrieved, k, embedding model)

What follows from that:
- Publishing a new version of anything the asker can read gives a new key, so a stale answer
  is never served. Nothing has to be found and deleted: the old entry just never matches again.
- A change of access (a grant, a revocation, an archive, a delete) changes the readable set,
  which also gives a new key.
- Two people share an entry only if they can read exactly the same versions. A cached answer
  therefore never shows anyone something that asking afresh wouldn't have shown them.

The key is deliberately coarse. A precise one ("only the versions this answer used") would hit
more often, but a newly readable table can change which table the planner picks, and a precise
key would miss that. Correct first; hit rate second.

Only first questions are cached. A follow-up ("And April?") means something different in each
conversation. Only verified answers are cached: never a fallback, a clarification or an error.
"""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.answer import planner as planner_prompt
from app.answer import writer as writer_prompt
from app.authz import readable_sources_query
from app.config import get_settings
from app.llm.client import LLMClient
from app.models import AnswerCacheEntry, AnswerRun, SourceVersion, User, VersionStatus

KEY_VERSION = "cache-v2"  # bump to invalidate every entry if the key's recipe changes


def normalise(question: str) -> str:
    """Case, spacing and a trailing question mark don't make a different question."""
    return re.sub(r"\s+", " ", question).strip().lower().rstrip("?.! ")


def readable_versions(db: Session, user: User) -> list[tuple[str, int]]:
    """Every source the user can read now, with its published version (0 if none yet)."""
    if not user.is_active:
        return []
    readable = readable_sources_query(user).subquery()
    rows = db.execute(
        select(readable.c.slug, SourceVersion.version)
        .select_from(readable)
        .outerjoin(
            SourceVersion,
            (SourceVersion.source_id == readable.c.id)
            & (SourceVersion.status == VersionStatus.PUBLISHED),
        )
    )
    return sorted((slug, version or 0) for slug, version in rows)


def _models(llm: LLMClient) -> list[str]:
    return [f"{p.name}:{p.model}" for p in llm.providers]


def cache_key(
    question: str, readable: list[tuple[str, int]], planner: LLMClient, writer: LLMClient
) -> str:
    settings = get_settings()
    recipe = {
        "v": KEY_VERSION,
        "question": normalise(question),
        "readable": readable,
        "planner": [_models(planner), planner_prompt.PROMPT_VERSION],
        "writer": [_models(writer), writer_prompt.PROMPT_VERSION],
        # Which tables the planner is shown changes what it can choose, so it's part of the key.
        "schema": [settings.schema_mode, settings.schema_top_k, settings.embedding_model],
    }
    return hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()


def lookup(db: Session, key: str, readable: list[tuple[str, int]]) -> AnswerRun | None:
    """The saved run to reuse, or None. The key already guarantees the asker can read every
    version the run used; the checks below say so again, so a mistake in the key can't leak."""
    ttl = timedelta(hours=get_settings().answer_cache_ttl_hours)
    entry = db.get(AnswerCacheEntry, key)
    if entry is None or entry.created_at < datetime.now(UTC) - ttl:
        return None
    run = db.get(AnswerRun, entry.answer_run_id)
    allowed = set(readable)
    if (
        run is None
        or run.status != "answered"
        or run.redacted_reason is not None
        or any((s["source"], s["version"]) not in allowed for s in run.source_versions)
    ):
        return None
    db.execute(
        update(AnswerCacheEntry)
        .where(AnswerCacheEntry.key == key)
        .values(hits=AnswerCacheEntry.hits + 1, last_hit_at=datetime.now(UTC))
    )
    return run


def store(db: Session, key: str, run_id: uuid.UUID) -> None:
    """Remember a verified answer. If two people asked at once, the first one stays."""
    db.execute(
        insert(AnswerCacheEntry)
        .values(key=key, answer_run_id=run_id, hits=0)
        .on_conflict_do_nothing(index_elements=["key"])
    )


def forget_runs(db: Session, run_ids: list[uuid.UUID]) -> int:
    """Drop entries pointing at these runs (their content is being erased)."""
    if not run_ids:
        return 0
    result = db.execute(delete(AnswerCacheEntry).where(AnswerCacheEntry.answer_run_id.in_(run_ids)))
    return result.rowcount or 0


def purge_expired(db: Session) -> int:
    """Remove entries past their lifetime. Run by the worker's periodic sweep. Correctness never
    depends on it (an expired entry is ignored anyway), only tidiness."""
    ttl = timedelta(hours=get_settings().answer_cache_ttl_hours)
    result = db.execute(
        delete(AnswerCacheEntry).where(AnswerCacheEntry.created_at < datetime.now(UTC) - ttl)
    )
    db.commit()
    return result.rowcount or 0
