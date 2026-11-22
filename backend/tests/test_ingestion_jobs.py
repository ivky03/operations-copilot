"""Asynchronous ingestion: uploads, the job queue, the worker, its failure paths, and publishing.

These run against real Postgres and the Floci S3/SQS emulator, with fresh queues per test
(`ingest_queues`), so messages, redelivery and the dead-letter queue behave as in AWS.
The worker's functions are called directly here, one batch at a time, except in the crash
test, which runs a real worker process and kills it mid-job.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.aws import get_client
from app.cli import REFERENCE, publish_reference, seed_reference
from app.ingest import jobs
from app.ingest import publish as publish_module
from app.models import Job, JobStatus, Source, SourceVersion, VersionStatus
from app.worker import Worker
from tests.conftest import login

pytestmark = pytest.mark.integration

STEWARD = "steward@example.com"
MANIFESTS, TABLES, DOCS = REFERENCE / "manifests", REFERENCE / "tables", REFERENCE / "documents"
STORES_CSV = (TABLES / "stores.csv").read_bytes()
NEW_STORE = b"S09,Dublin Store,Ireland,UK & Ireland,Retail,2026-06-01\n"


@pytest.fixture
def setup(db, make_user, client, ingest_queues):
    seed_reference(db)
    publish_reference(db)
    make_user(STEWARD, role="steward", groups=("operations", "finance"))
    csrf = login(client, STEWARD)
    client.headers["X-CSRF-Token"] = csrf
    return client


def upload_stores(client, csv: bytes = STORES_CSV + NEW_STORE, manifest: str = "stores"):
    files = {
        "manifest": ("stores.yaml", (MANIFESTS / f"{manifest}.yaml").read_bytes()),
        "data": ("stores.csv", csv),
    }
    resp = client.post("/api/steward/sources/stores/uploads", files=files)
    assert resp.status_code == 202, resp.text
    return resp.json()


def next_document(
    doc_id: str = "shipping-and-delivery", version: int = 2, extra: str = ""
) -> bytes:
    raw = (DOCS / f"{doc_id}.v1.md").read_text(encoding="utf-8")
    raw = raw.replace("version: 1", f"version: {version}", 1)
    return (raw + extra).encode()


def work(n: int = 1) -> int:
    """Run the worker until it has handled n messages (or nothing more arrives)."""
    worker, handled = Worker(), 0
    for _ in range(n * 3):
        handled += worker.run_once(wait_seconds=1)
        if handled >= n:
            break
    return handled


def job_row(db, job_id) -> Job:
    db.expire_all()
    return db.get(Job, job_id)


def queue_depth(url: str) -> int:
    attrs = get_client("sqs").get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return int(attrs["ApproximateNumberOfMessages"]) + int(
        attrs["ApproximateNumberOfMessagesNotVisible"]
    )


def dataset_tables(db) -> set[str]:
    return set(
        db.scalars(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema='datasets'")
        )
    )


# ----------------------------------------------------------------------------- access


def test_uploads_need_a_steward_who_can_read_the_source(db, make_user, client, ingest_queues):
    seed_reference(db)
    files = {"document": ("x.md", next_document())}
    url = "/api/steward/sources/shipping-and-delivery/uploads"
    assert client.post(url, files=files).status_code == 401

    make_user("emp@example.com", groups=("operations",))
    client.headers["X-CSRF-Token"] = login(client, "emp@example.com")
    assert client.post(url, files=files).status_code == 403

    # A steward outside the source's groups gets 404: existence isn't confirmed.
    make_user("fin-steward@example.com", role="steward", groups=("finance",))
    client.headers["X-CSRF-Token"] = login(client, "fin-steward@example.com")
    assert client.post(url, files=files).status_code == 404
    assert client.get("/api/steward/sources").json() == [
        s for s in client.get("/api/steward/sources").json() if s["slug"] != "shipping-and-delivery"
    ]

    del client.headers["X-CSRF-Token"]  # no CSRF token: refused before anything happens
    assert client.post(url, files=files).status_code == 403
    assert db.scalar(select(func.count()).select_from(Job)) == 0


# ----------------------------------------------------------------------------- happy paths


def test_a_dataset_upload_is_prepared_in_the_background_and_published_after_preview(setup, db):
    client = setup
    job = upload_stores(client)
    assert job["status"] == "pending"
    stored = get_client("s3").get_object(
        Bucket="opscopilot-uploads-test", Key=f"uploads/stores/{job['id']}/data"
    )
    assert stored["Body"].read().endswith(NEW_STORE)

    assert work() == 1
    job = client.get(f"/api/steward/jobs/{job['id']}").json()
    assert (job["status"], job["result_status"], job["version"], job["row_count"]) == (
        "succeeded",
        "ready",
        2,
        9,
    )
    # Ready is not published: everyone still sees v1.
    assert client.get("/api/sources/stores").json()["published_version"] == 1

    preview = client.get("/api/steward/sources/stores/versions/2").json()
    assert preview["version"]["status"] == "ready"
    assert ["S09", "Dublin Store", "Ireland", "UK & Ireland", "Retail", "2026-06-01"] in preview[
        "sample_rows"
    ]
    assert preview["compared_to"]["published_row_count"] == 8

    published = client.post("/api/steward/sources/stores/versions/2/publish")
    assert published.status_code == 200, published.text
    assert client.get("/api/sources/stores").json()["published_version"] == 2
    statuses = [
        (v["version"], v["status"])
        for v in client.get("/api/steward/sources/stores/versions").json()
    ]
    assert statuses == [(2, "published"), (1, "superseded")]


def test_a_document_upload_is_chunked_previewed_and_published(setup, db):
    client = setup
    extra = "\n## Weekend deliveries\nDrone deliveries are trialled in Ireland from July.\n"
    resp = client.post(
        "/api/steward/sources/shipping-and-delivery/uploads",
        files={"document": ("s.md", next_document(extra=extra))},
    )
    assert resp.status_code == 202
    work()
    preview = client.get("/api/steward/sources/shipping-and-delivery/versions/2").json()
    assert preview["version"]["status"] == "ready" and preview["version"]["chunks"] > 0
    assert preview["compared_to"]["sections_added"] == ["Weekend deliveries"]

    from app.documents.search import search
    from app.models import User

    steward = db.scalar(select(User).where(User.email == STEWARD))
    # "Drone" appears in no reference document: only the new version could match.
    assert not any("Drone" in h.content for h in search(db, steward, "drone deliveries"))
    client.post("/api/steward/sources/shipping-and-delivery/versions/2/publish")
    db.expire_all()
    assert any("Drone" in h.content for h in search(db, steward, "drone deliveries"))


def test_invalid_files_are_reported_row_by_row(setup, db):
    client = setup
    files = {
        "manifest": ("t.yaml", (MANIFESTS / "transactions.yaml").read_bytes()),
        "data": (
            "t.csv",
            (REFERENCE / "invalid_uploads" / "transactions_with_errors.csv").read_bytes(),
        ),
    }
    job = client.post("/api/steward/sources/transactions/uploads", files=files).json()
    work()
    job = client.get(f"/api/steward/jobs/{job['id']}").json()
    # The job succeeded (the file was processed); the file itself is invalid.
    assert (job["status"], job["result_status"]) == ("succeeded", "invalid")
    assert job["issue_count"] >= 5 and any("row" in i for i in job["issues"])
    detail = client.get(f"/api/steward/sources/transactions/versions/{job['version']}").json()
    assert detail["version"]["status"] == "invalid" and detail["issues"] == job["issues"]


def test_a_manifest_for_another_source_is_rejected(setup, db):
    client = setup
    files = {
        "manifest": ("p.yaml", (MANIFESTS / "products.yaml").read_bytes()),
        "data": ("p.csv", (TABLES / "products.csv").read_bytes()),
    }
    job = client.post("/api/steward/sources/stores/uploads", files=files).json()
    work()
    job = client.get(f"/api/steward/jobs/{job['id']}").json()
    assert job["result_status"] == "invalid"
    assert "for 'products', but it was uploaded to 'stores'" in job["issues"][0]


# ----------------------------------------------------------------------------- failure paths


def test_a_duplicate_message_creates_one_version(setup, db, ingest_queues):
    job = upload_stores(setup)
    # SQS delivers "at least once": simulate the same message arriving twice.
    get_client("sqs").send_message(
        QueueUrl=ingest_queues.main, MessageBody=json.dumps({"job_id": job["id"]})
    )
    assert work(2) == 2
    row = job_row(db, job["id"])
    assert (row.status, row.attempts) == (JobStatus.SUCCEEDED, 1)
    versions = db.scalar(
        select(func.count()).select_from(SourceVersion).join(Source).where(Source.slug == "stores")
    )
    assert versions == 2  # v1 (reference) + exactly one new version
    assert queue_depth(ingest_queues.main) == 0


def test_an_error_mid_job_leaves_nothing_behind_and_the_retry_succeeds(setup, db, monkeypatch):
    monkeypatch.setattr(jobs, "backoff_seconds", lambda attempts: 0)
    real = publish_module._build_value_index
    calls = {"n": 0}

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # after CREATE TABLE and COPY, before the job is marked done
            raise RuntimeError("simulated failure while indexing")
        return real(*args, **kwargs)

    monkeypatch.setattr(publish_module, "_build_value_index", fail_once)
    tables_before = dataset_tables(db)
    job = upload_stores(setup)
    work()
    row = job_row(db, job["id"])
    assert (row.status, row.attempts) == (JobStatus.PENDING, 1)
    assert "simulated failure" in row.error
    assert dataset_tables(db) == tables_before  # the CREATE TABLE was rolled back too
    assert (
        db.scalar(
            select(func.count())
            .select_from(SourceVersion)
            .where(SourceVersion.status == VersionStatus.READY)
        )
        == 0
    )

    work()  # the same message comes back after the (zero) backoff
    row = job_row(db, job["id"])
    assert (row.status, row.attempts, row.error) == (JobStatus.SUCCEEDED, 2, None)


def test_after_repeated_failures_the_job_fails_and_a_steward_can_retry(
    setup, db, monkeypatch, ingest_queues
):
    monkeypatch.setattr(jobs, "backoff_seconds", lambda attempts: 0)
    real = jobs._download

    def broken(job):
        raise ConnectionError("storage unavailable")

    monkeypatch.setattr(jobs, "_download", broken)
    job = upload_stores(setup)
    work(3)
    row = job_row(db, job["id"])
    assert (row.status, row.attempts) == (JobStatus.FAILED, 3)
    assert queue_depth(ingest_queues.main) == 0  # given up: the message is deleted
    assert setup.post(f"/api/steward/jobs/{job['id']}/retry").json()["status"] == "pending"

    monkeypatch.setattr(jobs, "_download", real)  # storage is back
    work()
    assert job_row(db, job["id"]).status == JobStatus.SUCCEEDED
    assert setup.post(f"/api/steward/jobs/{job['id']}/retry").status_code == 409


def test_a_lost_send_is_recovered_by_the_sweeper(setup, db, monkeypatch, ingest_queues):
    sqs = get_client("sqs")
    real_send = sqs.send_message

    def lost(**kwargs):
        raise ConnectionError("network blip")

    sqs.send_message = lost
    try:
        job = upload_stores(setup)  # the upload still succeeds: the job is committed first
    finally:
        sqs.send_message = real_send
    assert job_row(db, job["id"]).enqueued_at is None
    assert queue_depth(ingest_queues.main) == 0

    assert jobs.sweep(db) == 1  # never sent: re-sent straight away
    assert jobs.sweep(db) == 0  # sent a moment ago: not stale yet, left alone
    work()
    assert job_row(db, job["id"]).status == JobStatus.SUCCEEDED


def test_a_message_that_keeps_killing_the_worker_goes_to_the_dead_letter_queue(
    setup, db, ingest_queues
):
    job = upload_stores(setup)
    sqs = get_client("sqs")
    # A worker that dies right after receiving never deletes or records anything. Simulate
    # 5 such deaths (ingest_max_receives); each time the message returns after the timeout.
    for _ in range(5):
        deadline = time.time() + 10
        while not sqs.receive_message(QueueUrl=ingest_queues.main, WaitTimeSeconds=1).get(
            "Messages"
        ):
            assert time.time() < deadline, "message did not come back"
    time.sleep(3.5)
    assert (
        sqs.receive_message(QueueUrl=ingest_queues.main, WaitTimeSeconds=1).get("Messages") is None
    )
    assert jobs.drain_dead_letters(db) == 1
    row = job_row(db, job["id"])
    assert row.status == JobStatus.FAILED and "stopped while processing" in row.error


def test_a_worker_killed_mid_job_leaves_nothing_and_another_worker_finishes(
    setup, db, ingest_queues
):
    """A real worker process is killed (SIGKILL: no cleanup code runs) while it's inside the
    job's transaction, after the table was created. Postgres rolls the transaction back when
    the connection drops; the job's lock and the message's visibility both expire; the next
    worker claims it and finishes."""
    tables_before = dataset_tables(db)
    job = upload_stores(setup)
    script = (
        "import time\n"
        "from app.ingest import publish\n"
        "def hang(*a, **k):\n"
        "    print('inside-transaction', flush=True); time.sleep(60)\n"
        "publish._build_value_index = hang\n"
        "from app.worker import Worker\n"
        "Worker().run_once(wait_seconds=5)\n"
    )
    proc = subprocess.Popen(  # noqa: S603 - our own fixed script, run with this interpreter
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "inside-transaction"
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    row = job_row(db, job["id"])
    assert (row.status, row.attempts) == (JobStatus.RUNNING, 1)  # claimed, never finished
    assert dataset_tables(db) == tables_before
    time.sleep(3.5)  # lock and visibility timeout (3 s in tests) expire
    work()
    row = job_row(db, job["id"])
    assert (row.status, row.attempts) == (JobStatus.SUCCEEDED, 2)
    assert row.version.status == VersionStatus.READY


# ----------------------------------------------------------------------------- publish rules


def test_publishing_can_not_break_datasets_that_depend_on_this_one(setup, db):
    client = setup
    without_s01 = b"".join(
        line for line in STORES_CSV.splitlines(keepends=True) if not line.startswith(b"S01,")
    )
    upload_stores(client, csv=without_s01)
    work()
    resp = client.post("/api/steward/sources/stores/versions/2/publish")
    assert resp.status_code == 409
    assert "transactions (v1) uses store_id 'S01'" in resp.json()["detail"]
    assert client.get("/api/sources/stores").json()["published_version"] == 1

    tables_before = dataset_tables(db)
    assert client.post("/api/steward/sources/stores/versions/2/discard").json()["status"] == (
        "discarded"
    )
    assert len(dataset_tables(db)) == len(tables_before) - 1  # its loaded table is dropped
    assert client.post("/api/steward/sources/stores/versions/2/publish").status_code == 409


def test_an_older_ready_version_can_not_replace_a_newer_published_one(setup, db):
    client = setup
    url = "/api/steward/sources/shipping-and-delivery"
    client.post(f"{url}/uploads", files={"document": ("a.md", next_document(extra="\nA.\n"))})
    work()
    client.post(
        f"{url}/uploads", files={"document": ("b.md", next_document(version=3, extra="\nB.\n"))}
    )
    work()
    assert [
        s["ready_versions"]
        for s in client.get("/api/steward/sources").json()
        if s["slug"] == "shipping-and-delivery"
    ] == [[2, 3]]
    assert client.post(f"{url}/versions/3/publish").status_code == 200
    resp = client.post(f"{url}/versions/2/publish")
    assert resp.status_code == 409 and "newer version (v3)" in resp.json()["detail"]


def test_re_uploading_the_same_files_creates_nothing_new(setup, db):
    client = setup
    upload_stores(client)
    upload_stores(client)
    work(2)
    results = [
        (j["status"], j["result_status"], j["version"])
        for j in client.get("/api/steward/jobs?source=stores").json()
    ]
    assert results == [("succeeded", "ready", 2), ("succeeded", "ready", 2)]
