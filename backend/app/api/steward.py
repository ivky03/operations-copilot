"""Steward endpoints: upload new versions, follow their processing, preview, publish or discard.

Rules, enforced here on the server:
- Only stewards (role) can call these routes (403 otherwise; 401 if not logged in).
- A steward manages only sources they can read through their groups. Other sources return
  404, the same as a source that doesn't exist, so their existence isn't confirmed.
- A steward can archive a source (hidden from every answer, reversible) and restore it.
- Registering new sources, changing who can read them and deleting them permanently stay with
  administrators.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.authz import managed_sources_query, readable_sources_query, require_role
from app.config import get_settings
from app.db import get_db
from app.ingest import jobs, lifecycle
from app.ingest.publish import DATASET_SCHEMA, PublishError, discard_version, publish_version
from app.models import (
    DocChunk,
    DocumentContent,
    Job,
    JobStatus,
    Role,
    Source,
    SourceKind,
    SourceStatus,
    SourceVersion,
    User,
    VersionStatus,
)

require_steward = require_role(Role.STEWARD)
router = APIRouter(prefix="/api/steward", tags=["steward"], dependencies=[Depends(require_steward)])

PREVIEW_ROWS = 20


# ----------------------------------------------------------------------------- shapes


class VersionOut(BaseModel):
    version: int
    status: str
    created_at: datetime
    published_at: datetime | None
    row_count: int | None = None  # datasets
    chunks: int | None = None  # documents
    issue_count: int = 0

    @classmethod
    def of(cls, v: SourceVersion) -> "VersionOut":
        return cls(
            version=v.version,
            status=v.status.value,
            created_at=v.created_at,
            published_at=v.published_at,
            row_count=v.meta.get("row_count"),
            chunks=v.meta.get("chunks"),
            issue_count=v.meta.get("issue_count", 0),
        )


class StewardSourceOut(BaseModel):
    slug: str
    kind: str
    title: str
    status: str  # active | archived
    used_by: list[str]  # datasets (that you can see) linking to this one
    published_version: int | None
    ready_versions: list[int]  # prepared, waiting for a publish decision
    active_jobs: int  # pending or running


class JobOut(BaseModel):
    id: uuid.UUID
    source: str
    kind: str
    status: str
    attempts: int
    max_attempts: int
    error: str | None
    result_status: str | None  # ready | unchanged | invalid, once succeeded
    version: int | None
    issues: list[str]
    issue_count: int
    row_count: int | None
    created_by: str | None
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, j: Job) -> "JobOut":
        r = j.result or {}
        return cls(
            id=j.id,
            source=j.source.slug,
            kind="dataset" if j.kind == jobs.PREPARE_DATASET else "document",
            status=j.status.value,
            attempts=j.attempts,
            max_attempts=j.max_attempts,
            error=j.error,
            result_status=r.get("status"),
            version=r.get("version"),
            issues=r.get("issues", []),
            issue_count=r.get("issue_count", 0),
            row_count=r.get("row_count"),
            created_by=j.creator.display_name if j.creator else None,
            created_at=j.created_at,
            finished_at=j.finished_at,
        )


class ColumnOut(BaseModel):
    name: str
    type: str
    description: str


class Comparison(BaseModel):
    """How a version differs from the one currently published."""

    published_version: int
    published_row_count: int | None = None
    columns_added: list[str] = []
    columns_removed: list[str] = []
    sections_added: list[str] = []
    sections_removed: list[str] = []


class VersionDetail(BaseModel):
    source: str
    kind: str
    title: str
    version: VersionOut
    issues: list[str] = []
    # Datasets
    columns: list[ColumnOut] = []
    sample_rows: list[list[str | None]] = []
    # Documents
    doc_title: str | None = None
    effective_date: str | None = None
    sections: list[str] = []
    body: str | None = None
    compared_to: Comparison | None = None


# ----------------------------------------------------------------------------- helpers


def _source(db: Session, user: User, slug: str, active: bool = True) -> Source:
    """A source this steward manages. `active=True` for anything that changes its content:
    an archived source must be restored first."""
    source = db.scalar(managed_sources_query(user).where(Source.slug == slug))
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found.")
    if active and source.status != SourceStatus.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, "This source is archived. Restore it first.")
    return source


def _used_by(db: Session, user: User) -> dict[str, list[str]]:
    """{dataset: datasets linking to it}, limited to datasets this steward can read: naming a
    restricted dataset here would reveal that it exists."""
    readable = {s.slug for s in db.scalars(readable_sources_query(user))}
    return {
        target: [d for d in deps if d in readable]
        for target, deps in lifecycle.links_to(db).items()
    }


def _version(db: Session, source: Source, number: int) -> SourceVersion:
    v = db.scalar(
        select(SourceVersion).where(
            SourceVersion.source_id == source.id, SourceVersion.version == number
        )
    )
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found.")
    return v


def _job(db: Session, user: User, job_id: uuid.UUID) -> Job:
    managed = managed_sources_query(user).with_only_columns(Source.id)
    job = db.scalar(select(Job).where(Job.id == job_id, Job.source_id.in_(managed)))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    return job


def _read_upload(f: UploadFile | None, label: str) -> bytes:
    if f is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Missing file: {label}.")
    limit = get_settings().max_upload_bytes
    content = f.file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"{label} is larger than {limit // 2**20} MB."
        )
    if not content.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{label} is empty.")
    return content


def _sections(db: Session, version: SourceVersion) -> list[str]:
    return list(
        db.scalars(
            select(DocChunk.heading)
            .where(DocChunk.source_version_id == version.id)
            .order_by(DocChunk.ordinal)
        )
    )


def _sample_rows(db: Session, version: SourceVersion) -> list[list[str | None]]:
    manifest = version.meta["manifest"]
    names = [c["name"] for c in manifest["columns"]]
    cols = ", ".join(f'"{n}"' for n in names)
    order = ", ".join(f'"{k}"' for k in manifest["primary_key"])
    rows = db.execute(
        text(
            f'SELECT {cols} FROM {DATASET_SCHEMA}."{version.meta["table"]}" '  # noqa: S608 - names from the validated manifest, table generated by code
            f"ORDER BY {order} LIMIT {PREVIEW_ROWS}"
        )
    )
    return [[None if v is None else str(v) for v in row] for row in rows]


def _published(db: Session, source: Source) -> SourceVersion | None:
    return db.scalar(
        select(SourceVersion).where(
            SourceVersion.source_id == source.id, SourceVersion.status == VersionStatus.PUBLISHED
        )
    )


# ----------------------------------------------------------------------------- sources, versions


@router.get("/sources")
def list_sources(
    user: User = Depends(require_steward), db: Session = Depends(get_db)
) -> list[StewardSourceOut]:
    sources = list(db.scalars(managed_sources_query(user).order_by(Source.slug)))
    ids = [s.id for s in sources]
    versions = list(db.scalars(select(SourceVersion).where(SourceVersion.source_id.in_(ids))))
    active = list(
        db.scalars(
            select(Job.source_id).where(
                Job.source_id.in_(ids), Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING])
            )
        )
    )
    used_by = _used_by(db, user)
    out = []
    for s in sources:
        mine = [v for v in versions if v.source_id == s.id]
        published = next((v.version for v in mine if v.status == VersionStatus.PUBLISHED), None)
        out.append(
            StewardSourceOut(
                slug=s.slug,
                kind=s.kind.value,
                title=s.title,
                status=s.status.value,
                used_by=used_by.get(s.slug, []),
                published_version=published,
                ready_versions=sorted(v.version for v in mine if v.status == VersionStatus.READY),
                active_jobs=active.count(s.id),
            )
        )
    return out


@router.get("/sources/{slug}/versions")
def list_versions(
    slug: str, user: User = Depends(require_steward), db: Session = Depends(get_db)
) -> list[VersionOut]:
    source = _source(db, user, slug, active=False)
    rows = db.scalars(
        select(SourceVersion)
        .where(SourceVersion.source_id == source.id)
        .order_by(SourceVersion.version.desc())
    )
    return [VersionOut.of(v) for v in rows]


@router.get("/sources/{slug}/versions/{number}")
def get_version(
    slug: str, number: int, user: User = Depends(require_steward), db: Session = Depends(get_db)
) -> VersionDetail:
    """Everything needed to decide whether to publish: issues, or a preview and what changed."""
    source = _source(db, user, slug, active=False)
    v = _version(db, source, number)
    detail = VersionDetail(
        source=source.slug,
        kind=source.kind.value,
        title=source.title,
        version=VersionOut.of(v),
        issues=v.meta.get("issues", []),
    )
    has_content = v.status in (
        VersionStatus.READY,
        VersionStatus.PUBLISHED,
        VersionStatus.SUPERSEDED,
    )
    if not has_content:
        return detail
    current = _published(db, source)
    compare = current is not None and current.id != v.id
    if source.kind == SourceKind.DATASET:
        columns = v.meta["manifest"]["columns"]
        detail.columns = [
            ColumnOut(**{k: c[k] for k in ("name", "type", "description")}) for c in columns
        ]
        detail.sample_rows = _sample_rows(db, v)
        if compare:
            new = {c["name"] for c in columns}
            old = {c["name"] for c in current.meta["manifest"]["columns"]}
            detail.compared_to = Comparison(
                published_version=current.version,
                published_row_count=current.meta.get("row_count"),
                columns_added=sorted(new - old),
                columns_removed=sorted(old - new),
            )
    else:
        detail.doc_title = v.meta.get("title")
        detail.effective_date = str(v.meta.get("effective_date"))
        detail.sections = _sections(db, v)
        content = db.get(DocumentContent, v.id)
        detail.body = content.body if content else None
        if compare:
            new, old = set(detail.sections), set(_sections(db, current))
            detail.compared_to = Comparison(
                published_version=current.version,
                sections_added=[s for s in detail.sections if s not in old],
                sections_removed=sorted(old - new),
            )
    return detail


@router.post("/sources/{slug}/versions/{number}/publish")
def publish(
    slug: str, number: int, user: User = Depends(require_steward), db: Session = Depends(get_db)
) -> VersionOut:
    """Make a ready version the one answers use. New answers use it from the next question."""
    source = _source(db, user, slug)
    v = _version(db, source, number)
    try:
        publish_version(db, v, user)
    except PublishError as e:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "; ".join(e.issues)) from e
    db.commit()
    return VersionOut.of(v)


@router.post("/sources/{slug}/versions/{number}/discard")
def discard(
    slug: str, number: int, user: User = Depends(require_steward), db: Session = Depends(get_db)
) -> VersionOut:
    source = _source(db, user, slug)
    v = _version(db, source, number)
    try:
        discard_version(db, v, user)
    except PublishError as e:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "; ".join(e.issues)) from e
    db.commit()
    return VersionOut.of(v)


# ----------------------------------------------------------------------------- archive, restore


def _summary(db: Session, user: User, source: Source) -> StewardSourceOut:
    return next(s for s in list_sources(user, db) if s.slug == source.slug)


@router.post("/sources/{slug}/archive")
def archive(
    slug: str, user: User = Depends(require_steward), db: Session = Depends(get_db)
) -> StewardSourceOut:
    """Hide a source from every answer, search and listing, for everyone. Nothing is deleted:
    restoring it brings it back as it was. Saved answers that used it are hidden meanwhile."""
    source = _source(db, user, slug)
    try:
        lifecycle.archive_source(db, source, user)
    except PublishError as e:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "; ".join(e.issues)) from e
    db.commit()
    return _summary(db, user, source)


@router.post("/sources/{slug}/restore")
def restore(
    slug: str, user: User = Depends(require_steward), db: Session = Depends(get_db)
) -> StewardSourceOut:
    source = _source(db, user, slug, active=False)
    try:
        lifecycle.restore_source(db, source, user)
    except PublishError as e:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "; ".join(e.issues)) from e
    db.commit()
    return _summary(db, user, source)


# ----------------------------------------------------------------------------- uploads, jobs


@router.post("/sources/{slug}/uploads", status_code=status.HTTP_202_ACCEPTED)
def upload(
    slug: str,
    manifest: Annotated[UploadFile | None, File(description="Dataset manifest (YAML)")] = None,
    data: Annotated[UploadFile | None, File(description="Dataset rows (CSV)")] = None,
    document: Annotated[UploadFile | None, File(description="Document (Markdown)")] = None,
    user: User = Depends(require_steward),
    db: Session = Depends(get_db),
) -> JobOut:
    """Accept the files and return at once (202 Accepted). Validation and loading happen in
    the background worker; follow progress with GET /api/steward/jobs/{id}."""
    source = _source(db, user, slug)
    if source.kind == SourceKind.DATASET:
        files = {"manifest": _read_upload(manifest, "manifest"), "data": _read_upload(data, "data")}
    else:
        files = {"document": _read_upload(document, "document")}
    job = jobs.submit_upload(db, user, source, files)
    return JobOut.of(job)


@router.get("/jobs")
def list_jobs(
    source: str | None = None,
    limit: int = 50,
    user: User = Depends(require_steward),
    db: Session = Depends(get_db),
) -> list[JobOut]:
    managed = managed_sources_query(user)
    if source:
        managed = managed.where(Source.slug == source)
    query = (
        select(Job)
        .where(Job.source_id.in_(managed.with_only_columns(Source.id)))
        .order_by(Job.created_at.desc())
        .limit(min(max(limit, 1), 200))
    )
    return [JobOut.of(j) for j in db.scalars(query)]


@router.get("/jobs/{job_id}")
def get_job(
    job_id: uuid.UUID, user: User = Depends(require_steward), db: Session = Depends(get_db)
) -> JobOut:
    return JobOut.of(_job(db, user, job_id))


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: uuid.UUID, user: User = Depends(require_steward), db: Session = Depends(get_db)
) -> JobOut:
    job = _job(db, user, job_id)
    if job.status != JobStatus.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only failed jobs can be retried.")
    return JobOut.of(jobs.retry(db, job, user))
