"""The end of a source's life: archive, restore, delete.

- **Archive** (steward, reversible): the source disappears from every answer, search, catalog
  and listing for everyone, because every read path only sees `active` sources (app.authz).
  Nothing is removed, so **restore** brings it back exactly as it was.
- **Delete** (administrator, permanent): every version's content is removed in ONE
  transaction: dataset tables dropped, value index, document text and search chunks deleted,
  version details erased. Saved answers that used the source are erased too, for every user,
  because they contain its rows and passages. What remains is a **tombstone**: the source row
  (slug, title, `deleted`, when) and its version rows with status `deleted`. The tombstone is
  why a late background job can see the source is gone instead of quietly recreating it, and
  why the slug can't be reused by accident.
  The raw uploaded files in S3 are removed after the commit. If that fails, the worker's
  sweeper retries, so a storage hiccup can't leave content behind for long.

Every function takes the source's row lock first, the same lock a worker takes before
preparing a version, so a delete and an upload being processed can't interleave: whichever
goes second sees what the first did.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import audit
from app.aws import get_client
from app.config import get_settings
from app.ingest.manifest import Manifest
from app.ingest.publish import (
    DATASET_SCHEMA,
    PublishError,
    _missing_references_now,
    _quote,
)
from app.models import (
    AnswerRun,
    DocChunk,
    DocumentContent,
    Job,
    JobStatus,
    Source,
    SourceGrant,
    SourceKind,
    SourceStatus,
    SourceVersion,
    User,
    ValueIndexEntry,
    VersionStatus,
)

log = structlog.get_logger()

SOURCE_DELETED = "source_deleted"
LOCK_TIMEOUT = "5s"


@dataclass
class DeleteReport:
    source: str
    versions: int = 0
    tables_dropped: int = 0
    answers_erased: int = 0
    jobs_closed: int = 0
    files_removed: int | None = None  # None if the S3 clean-up is still pending
    dependents: list[str] = field(default_factory=list)


def _lock(db: Session, source: Source) -> Source:
    locked = db.scalar(select(Source).where(Source.id == source.id).with_for_update())
    db.refresh(locked)  # read the status *after* taking the lock
    return locked


def links_to(db: Session) -> dict[str, list[str]]:
    """{dataset: [active datasets whose published version links to it]}, e.g.
    {"stores": ["returns", "transactions"]}."""
    rows = db.execute(
        select(Source.slug, SourceVersion.meta)
        .join(SourceVersion, SourceVersion.source_id == Source.id)
        .where(
            Source.status == SourceStatus.ACTIVE,
            Source.kind == SourceKind.DATASET,
            SourceVersion.status == VersionStatus.PUBLISHED,
        )
    ).all()
    found: dict[str, set[str]] = {}
    for slug, meta in rows:
        for rel in Manifest.model_validate(meta["manifest"]).relationships:
            if rel.target_source != slug:
                found.setdefault(rel.target_source, set()).add(slug)
    return {target: sorted(deps) for target, deps in found.items()}


def dependents(db: Session, slug: str) -> list[str]:
    """Datasets that link to `slug` (transactions → stores). They keep working when `slug`
    goes, but can no longer use its columns, so whoever removes it should be told."""
    return links_to(db).get(slug, [])


# ----------------------------------------------------------------------------- archive, restore


def archive_source(db: Session, source: Source, actor: User | None) -> None:
    """Hide the source from everything, reversibly. Doesn't commit."""
    source = _lock(db, source)
    if source.status != SourceStatus.ACTIVE:
        raise PublishError([f"{source.slug} is {source.status.value}, not active"])
    source.status = SourceStatus.ARCHIVED
    audit.record(db, actor, "source.archive", source.slug)


def restore_source(db: Session, source: Source, actor: User | None) -> None:
    """Make an archived source visible again. A dataset's links are re-checked first: what it
    links to may have changed while it was archived. Doesn't commit."""
    source = _lock(db, source)
    if source.status != SourceStatus.ARCHIVED:
        raise PublishError([f"{source.slug} is {source.status.value}, not archived"])
    published = db.scalar(
        select(SourceVersion).where(
            SourceVersion.source_id == source.id, SourceVersion.status == VersionStatus.PUBLISHED
        )
    )
    if source.kind == SourceKind.DATASET and published is not None:
        manifest = Manifest.model_validate(published.meta["manifest"])
        if issues := _missing_references_now(db, manifest, published.meta["table"]):
            raise PublishError(issues)
    source.status = SourceStatus.ACTIVE
    audit.record(db, actor, "source.restore", source.slug)


# ----------------------------------------------------------------------------- delete


def _erase_version(db: Session, version: SourceVersion) -> bool:
    """Remove one version's content. Returns whether a dataset table was dropped."""
    table = version.meta.get("table")
    if table:
        db.execute(text(f"DROP TABLE IF EXISTS {DATASET_SCHEMA}.{_quote(table)}"))
    db.query(ValueIndexEntry).filter(ValueIndexEntry.source_version_id == version.id).delete()
    db.query(DocChunk).filter(DocChunk.source_version_id == version.id).delete()
    db.query(DocumentContent).filter(DocumentContent.source_version_id == version.id).delete()
    # The details hold the manifest (column descriptions, alias values) or the document's
    # metadata and any validation issues, which quote rows. Only the number and status remain.
    version.meta = {}
    version.status = VersionStatus.DELETED
    return bool(table)


def _erase_answers(db: Session, slug: str) -> int:
    """Saved answers that used the source contain its rows and passages: erase their content
    for every user. The question stays, so a conversation still reads in order."""
    runs = db.scalars(
        select(AnswerRun).where(AnswerRun.source_versions.contains([{"source": slug}]))
    ).all()
    for run in runs:
        standalone = run.plan.get("standalone_question")
        run.answer = None
        run.evidence, run.passages, run.verification = [], [], {}
        run.plan = {"standalone_question": standalone} if standalone else {}
        run.redacted_reason = SOURCE_DELETED
    return len(runs)


def _close_jobs(db: Session, source: Source) -> int:
    """Open jobs are failed; every job's result is reduced to its outcome (issues quote rows)."""
    closed = 0
    now = datetime.now(UTC)
    for job in db.scalars(select(Job).where(Job.source_id == source.id)):
        if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
            job.status, job.locked_until, job.finished_at = JobStatus.FAILED, None, now
            job.error = "The source was deleted."
            closed += 1
        elif job.error:
            job.error = "Erased with the source."  # error messages can quote the file
        job.result = {k: v for k, v in (job.result or {}).items() if k in ("status", "version")}
    return closed


def delete_source(db: Session, source: Source, actor: User | None) -> DeleteReport:
    """Permanently remove the source's content and leave a tombstone. Doesn't commit, and
    doesn't touch S3: call purge_uploads() after the commit."""
    # Dropping a table waits for every query reading it. Wait a few seconds at most, then give
    # up (SourceBusyError) rather than queue every later reader behind a stuck delete.
    db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    source = _lock(db, source)
    if source.status == SourceStatus.DELETED:
        raise PublishError([f"{source.slug} was already deleted"])
    report = DeleteReport(source.slug, dependents=dependents(db, source.slug))
    versions = db.scalars(select(SourceVersion).where(SourceVersion.source_id == source.id)).all()
    for version in versions:
        report.tables_dropped += _erase_version(db, version)
    report.versions = len(versions)
    report.answers_erased = _erase_answers(db, source.slug)
    report.jobs_closed = _close_jobs(db, source)
    db.query(SourceGrant).filter(SourceGrant.source_id == source.id).delete()
    source.status = SourceStatus.DELETED
    source.deleted_at = datetime.now(UTC)
    audit.record(
        db,
        actor,
        "source.delete",
        source.slug,
        versions=report.versions,
        answers_erased=report.answers_erased,
    )
    return report


# ----------------------------------------------------------------------------- uploaded files


def _upload_prefix(slug: str) -> str:
    return f"uploads/{slug}/"  # where jobs.submit_upload stores a source's files


def purge_uploads(slug: str) -> int:
    """Delete every uploaded file of a source from S3. Idempotent. Returns how many."""
    s3, bucket = get_client("s3"), get_settings().upload_bucket
    removed = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=_upload_prefix(slug)):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            removed += len(keys)
    return removed


def try_purge_uploads(slug: str) -> int | None:
    """purge_uploads, but a failure is logged rather than raised: the delete is already
    committed, and the sweeper will finish the job."""
    try:
        return purge_uploads(slug)
    except Exception as e:  # any AWS/network error
        log.warning("purge_failed", source=slug, error=type(e).__name__)
        return None


def purge_deleted_uploads(db: Session) -> int:
    """Sweeper step: remove any files still stored for deleted sources."""
    slugs = db.scalars(select(Source.slug).where(Source.status == SourceStatus.DELETED)).all()
    removed = sum(purge_uploads(slug) for slug in slugs)
    if removed:
        log.info("purged_deleted_uploads", files=removed)
    return removed
