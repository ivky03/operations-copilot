"""Background processing of uploads: S3 for the files, SQS for delivery, Postgres for the truth.

Life of an upload:
  1. API: the files go to S3, a `jobs` row is committed (pending), THEN a message
     {"job_id": ...} is sent. If the send fails, the job is still safely recorded; the
     sweeper notices it was never delivered and sends it again (a lightweight "outbox").
  2. Worker: receives the message and *claims* the job with one atomic UPDATE. Only a pending
     job, or a running job whose lock has expired (its worker died), can be claimed. A
     duplicate message for a job someone else holds or already finished claims nothing and
     is simply deleted.
  3. Worker: downloads the files, prepares the version, and marks the job succeeded in the
     SAME transaction. So a crash at any point leaves either nothing or everything: never half
     a version, never a finished version with an unfinished job.
  4. On an error, the attempt is recorded and the message is made visible again after a
     backoff; after `max_attempts` the job is failed and a steward can retry it.
  5. A message whose worker dies outright, repeatedly (so nothing could be recorded), is moved
     by SQS to the dead-letter queue after `ingest_max_receives` receives. The worker drains
     that queue and marks those jobs failed, so nothing fails silently.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
import yaml
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app import audit
from app.aws import get_client
from app.config import get_settings
from app.ingest.publish import PublishResult, prepare_dataset, prepare_document
from app.models import Job, JobStatus, Source, SourceKind, SourceVersion, User

log = structlog.get_logger()

PREPARE_DATASET = "prepare_dataset"
PREPARE_DOCUMENT = "prepare_document"


# ----------------------------------------------------------------------------- infrastructure


@dataclass(frozen=True)
class Queues:
    main: str
    dlq: str


_queues: dict[str, Queues] = {}


def ensure_infrastructure() -> Queues:
    """Create the bucket, queue and dead-letter queue if missing, and (re)apply the queue
    settings. Idempotent; safe to call on every start."""
    s = get_settings()
    key = f"{s.upload_bucket}/{s.ingest_queue_name}"
    if key in _queues:
        return _queues[key]
    s3, sqs = get_client("s3"), get_client("sqs")
    existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    if s.upload_bucket not in existing:
        s3.create_bucket(Bucket=s.upload_bucket)
    dlq = sqs.create_queue(QueueName=s.ingest_dlq_name)["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    main = sqs.create_queue(QueueName=s.ingest_queue_name)["QueueUrl"]
    sqs.set_queue_attributes(
        QueueUrl=main,
        Attributes={
            "VisibilityTimeout": str(s.ingest_visibility_timeout),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(s.ingest_max_receives)}
            ),
        },
    )
    _queues[key] = Queues(main=main, dlq=dlq)
    return _queues[key]


# ----------------------------------------------------------------------------- submitting


def submit_upload(db: Session, user: User, source: Source, files: dict[str, bytes]) -> Job:
    """Store the files and create the job. `files` is {"manifest", "data"} for a dataset or
    {"document"} for a document. The message is sent after the commit; see enqueue()."""
    s = get_settings()
    ensure_infrastructure()  # make sure the bucket exists before writing to it
    job_id = uuid.uuid4()
    keys = {}
    for role, content in files.items():
        key = f"uploads/{source.slug}/{job_id}/{role}"
        get_client("s3").put_object(Bucket=s.upload_bucket, Key=key, Body=content)
        keys[role] = key
    job = Job(
        id=job_id,
        kind=PREPARE_DATASET if source.kind == SourceKind.DATASET else PREPARE_DOCUMENT,
        source_id=source.id,
        created_by=user.id,
        max_attempts=s.ingest_max_attempts,
        payload={"bucket": s.upload_bucket, "keys": keys},
    )
    db.add(job)
    audit.record(db, user, "upload.submit", source.slug, job=job_id)
    db.commit()
    enqueue(db, job)
    return job


def enqueue(db: Session, job: Job) -> bool:
    """Send the job's message. A failure is logged, not raised: the job is already committed,
    and the sweeper will send it later. Returns whether the send succeeded."""
    try:
        get_client("sqs").send_message(
            QueueUrl=ensure_infrastructure().main, MessageBody=json.dumps({"job_id": str(job.id)})
        )
    except Exception as e:  # any AWS/network error: the sweeper is the safety net
        log.warning("enqueue_failed", job=str(job.id), error=type(e).__name__)
        return False
    job.enqueued_at = datetime.now(UTC)
    db.commit()
    return True


def retry(db: Session, job: Job, user: User) -> Job:
    """A steward retries a failed job: fresh attempts, sent again."""
    job.status, job.attempts, job.locked_until, job.finished_at = JobStatus.PENDING, 0, None, None
    job.enqueued_at = None
    audit.record(db, user, "job.retry", job.source.slug, job=job.id)
    db.commit()
    enqueue(db, job)
    return job


# ----------------------------------------------------------------------------- processing


def claim(db: Session, job_id: uuid.UUID) -> Job | None:
    """Atomically take a job, or return None if it isn't available. One UPDATE statement, so
    two workers holding duplicate messages can't both win."""
    s = get_settings()
    now = datetime.now(UTC)
    claimed = db.scalar(
        update(Job)
        .where(
            Job.id == job_id,
            Job.attempts < Job.max_attempts,
            or_(
                Job.status == JobStatus.PENDING,
                (Job.status == JobStatus.RUNNING) & (Job.locked_until < now),
            ),
        )
        .values(
            status=JobStatus.RUNNING,
            attempts=Job.attempts + 1,
            locked_until=now + timedelta(seconds=s.ingest_visibility_timeout),
        )
        .returning(Job.id)
    )
    db.commit()
    return db.get(Job, claimed, populate_existing=True) if claimed else None


def _download(job: Job) -> dict[str, bytes]:
    s3 = get_client("s3")
    bucket = job.payload["bucket"]
    return {
        role: s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        for role, key in job.payload["keys"].items()
    }


def _prepare(db: Session, job: Job, files: dict[str, bytes], actor: User | None) -> PublishResult:
    slug = job.source.slug
    if job.kind == PREPARE_DATASET:
        try:
            manifest = yaml.safe_load(files["manifest"].decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as e:
            return PublishResult(
                slug, "invalid", issues=[f"manifest is not valid YAML: {e}"], issue_count=1
            )
        if not isinstance(manifest, dict):
            return PublishResult(
                slug, "invalid", issues=["manifest must be a YAML mapping"], issue_count=1
            )
        return prepare_dataset(db, manifest, files["data"], actor, expected_slug=slug)
    return prepare_document(db, files["document"], actor, expected_slug=slug)


def run(db: Session, job: Job) -> bool:
    """Process a claimed job. Returns True when the job is finished (succeeded or failed for
    good), so its message can be deleted; False when it should be tried again."""
    try:
        files = _download(job)
        result = _prepare(db, job, files, job.creator)
        job.status = JobStatus.SUCCEEDED
        job.result = {
            "status": result.status,
            "version": result.version,
            "issues": result.issues,
            "issue_count": result.issue_count,
            "row_count": result.row_count,
        }
        job.source_version_id = result.version_id
        if result.version_id is None and result.version is not None:
            job.source_version_id = _version_id(db, job.source_id, result.version)
        job.error, job.locked_until, job.finished_at = None, None, datetime.now(UTC)
        db.commit()  # the version and the job's success are saved together, or not at all
        log.info("job_succeeded", job=str(job.id), source=job.source.slug, result=result.status)
        return True
    except Exception as e:  # record any failure, then decide: retry later, or give up
        db.rollback()
        return _record_failure(db, job.id, e)


def _version_id(db: Session, source_id: uuid.UUID, number: int) -> uuid.UUID | None:
    return db.scalar(
        select(SourceVersion.id).where(
            SourceVersion.source_id == source_id, SourceVersion.version == number
        )
    )


def _record_failure(db: Session, job_id: uuid.UUID, error: Exception) -> bool:
    job = db.get(Job, job_id, populate_existing=True)
    message = f"{type(error).__name__}: {error}"[:500]
    job.error = message
    job.locked_until = None
    finished = job.attempts >= job.max_attempts
    job.status = JobStatus.FAILED if finished else JobStatus.PENDING
    if finished:
        job.finished_at = datetime.now(UTC)
    db.commit()
    log.warning("job_failed", job=str(job_id), attempt=job.attempts, final=finished, error=message)
    return finished


def backoff_seconds(attempts: int) -> int:
    """Wait before the next attempt: 10 s, 20 s, 40 s, … (capped at 5 minutes)."""
    return min(10 * 2 ** max(attempts - 1, 0), 300)


def handle_message(db: Session, message: dict) -> None:
    """Process one SQS message end to end, and delete it unless it should come back."""
    sqs, queues = get_client("sqs"), ensure_infrastructure()
    receipt = message["ReceiptHandle"]
    try:
        job_id = uuid.UUID(json.loads(message["Body"])["job_id"])
    except (ValueError, KeyError, TypeError):
        log.warning("bad_message_deleted", body=message.get("Body", "")[:200])
        sqs.delete_message(QueueUrl=queues.main, ReceiptHandle=receipt)
        return
    job = claim(db, job_id)
    if job is None:
        # Finished already, held by another worker (whose own message covers it), or unknown.
        log.info("message_without_claim_deleted", job=str(job_id))
        sqs.delete_message(QueueUrl=queues.main, ReceiptHandle=receipt)
        return
    if run(db, job):
        sqs.delete_message(QueueUrl=queues.main, ReceiptHandle=receipt)
    else:
        # Try again after a backoff: the same message reappears when its visibility ends.
        delay = backoff_seconds(job.attempts)
        sqs.change_message_visibility(
            QueueUrl=queues.main, ReceiptHandle=receipt, VisibilityTimeout=delay
        )
        # When the message is due back, so the sweeper doesn't mistake a job waiting out its
        # backoff for one whose message was lost.
        job.enqueued_at = datetime.now(UTC) + timedelta(seconds=delay)
        db.commit()


def receive(wait_seconds: int = 10, max_messages: int = 5) -> list[dict]:
    response = get_client("sqs").receive_message(
        QueueUrl=ensure_infrastructure().main,
        MaxNumberOfMessages=max_messages,
        WaitTimeSeconds=wait_seconds,  # long polling: one request waits for work, no busy loop
    )
    return response.get("Messages", [])


# ----------------------------------------------------------------------------- safety nets


def sweep(db: Session) -> int:
    """Re-send jobs that should be in the queue but may not be:
    - pending, never sent or not re-sent for a while (the send after the commit was lost);
    - running with a lock long expired (the worker died and its message is gone).
    Returns how many were re-sent. Re-sending is safe: claim() ignores duplicates."""
    s = get_settings()
    stale = datetime.now(UTC) - timedelta(seconds=s.ingest_stale_after_seconds)
    jobs = list(
        db.scalars(
            select(Job).where(
                or_(
                    (Job.status == JobStatus.PENDING)
                    & (or_(Job.enqueued_at.is_(None), Job.enqueued_at < stale)),
                    (Job.status == JobStatus.RUNNING) & (Job.locked_until < stale),
                )
            )
        )
    )
    sent = 0
    for job in jobs:
        if job.status == JobStatus.RUNNING and job.attempts >= job.max_attempts:
            _record_failure(db, job.id, RuntimeError("the worker stopped during the last attempt"))
            continue
        sent += enqueue(db, job)
    if jobs:
        log.info("sweep", found=len(jobs), resent=sent)
    return sent


def drain_dead_letters(db: Session) -> int:
    """Mark jobs whose messages reached the dead-letter queue as failed, then delete those
    messages. The steward sees why and can retry."""
    sqs, queues = get_client("sqs"), ensure_infrastructure()
    count = 0
    while True:
        messages = sqs.receive_message(
            QueueUrl=queues.dlq, MaxNumberOfMessages=10, WaitTimeSeconds=0
        ).get("Messages", [])
        if not messages:
            return count
        for m in messages:
            try:
                job = db.get(Job, uuid.UUID(json.loads(m["Body"])["job_id"]))
            except (ValueError, KeyError, TypeError):
                job = None
            if job is not None and job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                job.status, job.locked_until = JobStatus.FAILED, None
                job.finished_at = datetime.now(UTC)
                job.error = (
                    f"The worker stopped while processing this upload "
                    f"{get_settings().ingest_max_receives} times, so it was set aside. "
                    "Check the file, then retry."
                )
                db.commit()
                count += 1
            sqs.delete_message(QueueUrl=queues.dlq, ReceiptHandle=m["ReceiptHandle"])
