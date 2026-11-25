"""Archive, restore and delete (M7a): who may do it, what each does, and the awkward timings
(an upload being processed, a question reading the table, storage being unreachable).
What a hidden or deleted source must NOT reveal is tested in test_leakage.py."""

import threading

import pytest
import yaml
from sqlalchemy import select, text

from app.aws import get_client
from app.cli import REFERENCE, publish_reference, seed_reference
from app.config import get_settings
from app.db import _session_factory
from app.ingest import lifecycle
from app.ingest.publish import prepare_dataset
from app.models import (
    AuditLog,
    Job,
    JobStatus,
    Source,
    SourceStatus,
    SourceVersion,
    User,
    VersionStatus,
)
from app.query.catalog import Catalog, PlanError
from tests.conftest import login
from tests.test_ingestion_jobs import NEW_STORE, STORES_CSV, dataset_tables, upload_stores, work

pytestmark = pytest.mark.integration

ADMIN, STEWARD, OPS = "admin@example.com", "steward@example.com", "ops@example.com"


@pytest.fixture
def setup(db, make_user, ingest_queues):
    seed_reference(db)
    publish_reference(db)
    make_user(ADMIN, role="admin")
    make_user(STEWARD, role="steward", groups=("operations", "finance"))
    make_user(OPS, groups=("operations",))
    return db


def as_user(client, email):
    client.cookies.clear()
    client.headers["X-CSRF-Token"] = login(client, email)
    return client


def delete(client, slug, confirm=None):
    return client.post(f"/api/admin/sources/{slug}/delete", json={"confirm": confirm or slug})


def source(db, slug) -> Source:
    db.expire_all()
    return db.scalar(select(Source).where(Source.slug == slug))


def ops(db) -> User:
    return db.scalar(select(User).where(User.email == OPS))


# ----------------------------------------------------------------------------- who may


def test_only_stewards_of_the_source_archive_and_only_admins_delete(client, setup, make_user):
    make_user("emp@example.com", groups=("operations",))
    make_user("fin.steward@example.com", role="steward", groups=("finance",))
    archive = "/api/steward/sources/shipping-and-delivery/archive"  # operations only

    as_user(client, "emp@example.com")
    assert client.post(archive).status_code == 403
    as_user(client, "fin.steward@example.com")
    assert client.post(archive).status_code == 404  # not theirs: existence not confirmed
    as_user(client, ADMIN)
    assert client.post(archive).status_code == 403  # admins manage access, not content
    as_user(client, STEWARD)
    assert client.post(archive).status_code == 200
    assert delete(client, "shipping-and-delivery").status_code == 403

    as_user(client, ADMIN)
    assert delete(client, "shipping-and-delivery", confirm="shipping").status_code == 422
    assert delete(client, "no-such-source").status_code == 404
    assert delete(client, "shipping-and-delivery").status_code == 200
    assert delete(client, "shipping-and-delivery").status_code == 409  # already deleted
    actions = set(setup.scalars(select(AuditLog.action)))
    assert {"source.archive", "source.delete"} <= actions


# ----------------------------------------------------------------------------- archive, restore


def test_archive_hides_restore_brings_back_and_archived_sources_take_no_changes(client, setup):
    db = setup
    as_user(client, STEWARD)
    assert client.post("/api/steward/sources/returns/archive").json()["status"] == "archived"
    assert "returns.refund_value" not in Catalog.for_user(db, ops(db)).metric_names()
    assert client.post("/api/steward/sources/returns/archive").status_code == 409

    # Still listed for its steward (to restore it), but nothing about its content can change.
    listed = {s["slug"]: s for s in client.get("/api/steward/sources").json()}
    assert listed["returns"]["status"] == "archived"
    assert client.get("/api/steward/sources/returns/versions").status_code == 200
    files = {"manifest": ("m", b"x"), "data": ("d", b"x")}
    assert client.post("/api/steward/sources/returns/uploads", files=files).status_code == 409
    assert client.post("/api/steward/sources/returns/versions/1/publish").status_code == 409

    assert client.post("/api/steward/sources/returns/restore").json()["status"] == "active"
    assert "returns.refund_value" in Catalog.for_user(db, ops(db)).metric_names()
    assert client.post("/api/steward/sources/returns/restore").status_code == 409


def test_restore_rechecks_links_that_changed_while_archived(client, setup):
    """transactions is archived; meanwhile stores is republished without London (allowed, since
    no active dataset uses London any more). Restoring transactions would leave sales
    pointing at a store that's gone, so it's refused until stores has London again."""
    db = setup
    as_user(client, STEWARD)
    for slug in ("transactions", "returns"):
        assert client.post(f"/api/steward/sources/{slug}/archive").status_code == 200
    without_london = REFERENCE / "sample_uploads" / "stores_without_london.csv"
    upload_stores(client, csv=without_london.read_bytes())
    work()
    assert client.post("/api/steward/sources/stores/versions/2/publish").status_code == 200

    refused = client.post("/api/steward/sources/transactions/restore")
    assert refused.status_code == 409 and "S01" in refused.json()["detail"]
    assert source(db, "transactions").status == SourceStatus.ARCHIVED


def test_an_upload_waiting_when_its_source_is_archived_is_rejected_with_the_reason(client, setup):
    as_user(client, STEWARD)
    job = upload_stores(client)
    client.post("/api/steward/sources/stores/archive")
    work()
    done = client.get(f"/api/steward/jobs/{job['id']}").json()
    assert done["result_status"] == "invalid"
    assert done["issues"] == [
        "dataset 'stores' is archived; restore it before uploading a new version"
    ]


def test_archiving_a_linked_dataset_says_who_uses_it(client, setup):
    as_user(client, STEWARD)
    listed = {s["slug"]: s for s in client.get("/api/steward/sources").json()}
    assert listed["stores"]["used_by"] == ["returns", "transactions"]
    assert listed["transactions"]["used_by"] == []


# ----------------------------------------------------------------------------- delete


def test_delete_removes_every_version_and_leaves_a_tombstone(client, setup):
    db = setup
    as_user(client, STEWARD)
    upload_stores(client)  # v2, ready but not published: it must go too
    work()
    tables_before = dataset_tables(db)
    as_user(client, ADMIN)
    report = delete(client, "stores").json()
    assert report["versions"] == 2 and report["tables_dropped"] == 2
    assert report["dependents"] == ["returns", "transactions"]
    assert report["files_removed"] >= 2  # the v2 upload's manifest and data

    stores = source(db, "stores")
    assert stores.status == SourceStatus.DELETED and stores.deleted_at is not None
    assert stores.granted_groups == []
    versions = db.scalars(select(SourceVersion).where(SourceVersion.source_id == stores.id)).all()
    assert {v.status for v in versions} == {VersionStatus.DELETED}
    assert all(v.meta == {} for v in versions)
    assert len(tables_before - dataset_tables(db)) == 2
    listed = {s["slug"]: s for s in client.get("/api/admin/sources").json()}
    assert listed["stores"]["status"] == "deleted" and listed["stores"]["deleted_at"]

    # The name stays taken, and the tombstone can't be granted to anyone again.
    body = {"slug": "stores", "kind": "dataset", "title": "Stores", "groups": ["operations"]}
    reuse = client.post("/api/admin/sources", json=body)
    assert reuse.status_code == 409 and "can't be reused" in reuse.json()["detail"]
    grants = client.put("/api/admin/sources/stores/grants", json={"groups": ["operations"]})
    assert grants.status_code == 409


def test_datasets_linking_to_a_deleted_one_keep_working_without_its_columns(client, setup):
    db = setup
    as_user(client, ADMIN)
    delete(client, "stores")
    catalog = Catalog.for_user(db, ops(db))
    dataset, _ = catalog.find_metric("net_sales")
    assert dataset.slug == "transactions"
    with pytest.raises(PlanError, match="Unknown dimension 'country'"):
        catalog.resolve_dimension(dataset, "country")


def test_a_late_message_for_a_deleted_source_recreates_nothing(client, setup, ingest_queues):
    """The upload's message is still in the queue when the source is deleted. The worker must
    not bring any of it back."""
    db = setup
    as_user(client, STEWARD)
    job = upload_stores(client)
    as_user(client, ADMIN)
    assert delete(client, "stores").status_code == 200
    work()
    db.expire_all()
    stored = db.get(Job, job["id"])
    assert stored.status == JobStatus.FAILED and stored.error == "The source was deleted."
    stores = source(db, "stores")
    versions = db.scalars(select(SourceVersion).where(SourceVersion.source_id == stores.id)).all()
    assert [v.status for v in versions] == [VersionStatus.DELETED]
    assert not [t for t in dataset_tables(db) if t.startswith(f"t_{stores.id.hex[:12]}")]


def test_a_worker_that_locked_the_source_first_finishes_then_the_delete_erases_it(setup):
    """The other order: a worker is preparing a version (holding the source's lock) when the
    delete arrives. The delete waits for it, then erases the new version too."""
    db = setup
    worker = _session_factory()()
    manifest = yaml.safe_load((REFERENCE / "manifests/stores.yaml").read_text())
    prepared = prepare_dataset(worker, manifest, STORES_CSV + NEW_STORE)  # lock held, uncommitted
    assert prepared.status == "ready"
    stores_id = source(db, "stores").id
    finished = {}

    def run_delete():
        with _session_factory()() as other:
            finished["report"] = lifecycle.delete_source(other, other.get(Source, stores_id), None)
            other.commit()

    thread = threading.Thread(target=run_delete)
    thread.start()
    thread.join(1)
    assert thread.is_alive()  # waiting for the worker
    worker.commit()  # the worker finishes: v2 is ready
    worker.close()
    thread.join(10)
    assert finished["report"].versions == 2 and finished["report"].tables_dropped == 2
    db.expire_all()
    left = db.scalars(select(SourceVersion.status).where(SourceVersion.source_id == stores_id))
    assert set(left) == {VersionStatus.DELETED}


def test_a_delete_does_not_wait_forever_for_a_long_reader(client, setup, monkeypatch):
    db = setup
    monkeypatch.setattr(lifecycle, "LOCK_TIMEOUT", "300ms")
    table = db.scalar(
        select(SourceVersion.meta["table"].astext)
        .join(Source)
        .where(Source.slug == "products", SourceVersion.status == VersionStatus.PUBLISHED)
    )
    reader = _session_factory()()
    reader.execute(text(f'SELECT count(*) FROM datasets."{table}"'))  # noqa: S608 - still open
    try:
        as_user(client, ADMIN)
        busy = delete(client, "products")
        assert busy.status_code == 409 and "in use" in busy.json()["detail"]
        assert source(db, "products").status == SourceStatus.ACTIVE  # nothing half-done
    finally:
        reader.close()
    assert delete(client, "products").status_code == 200


def test_files_left_in_storage_by_a_failed_clean_up_are_removed_by_the_sweeper(
    client, setup, monkeypatch
):
    db = setup
    as_user(client, STEWARD)
    upload_stores(client)
    work()

    def unreachable(slug):
        raise ConnectionError("storage is down")

    monkeypatch.setattr(lifecycle, "purge_uploads", unreachable)
    as_user(client, ADMIN)
    assert delete(client, "stores").json()["files_removed"] is None
    monkeypatch.undo()

    s3, bucket = get_client("s3"), get_settings().upload_bucket
    assert s3.list_objects_v2(Bucket=bucket, Prefix="uploads/stores/").get("KeyCount")
    assert lifecycle.purge_deleted_uploads(db) >= 2
    assert not s3.list_objects_v2(Bucket=bucket, Prefix="uploads/stores/").get("KeyCount")


def test_only_the_turns_that_used_a_deleted_source_are_erased(client, setup):
    from app.answer.pipeline import answer_question
    from app.models import AnswerRun, Conversation
    from tests.test_answer_pipeline import IRELAND_MARCH, fake

    db = setup
    olivia = ops(db)
    chat = Conversation(user_id=olivia.id, title="Mixed")
    db.add(chat)
    db.commit()
    delivery = {"kind": "documents", "doc_searches": ["standard delivery time"]}
    for plan, answer in ((delivery, "2–4 business days [C1]."), (IRELAND_MARCH, "-€509.00 [D1].")):
        planner, _ = fake(plan)
        writer, _ = fake({"answer": answer})
        assert answer_question(db, olivia, "q?", planner, writer, chat).status == "answered"

    as_user(client, ADMIN)
    assert delete(client, "shipping-and-delivery").json()["answers_erased"] == 1
    as_user(client, OPS)
    turns = client.get(f"/api/conversations/{chat.id}").json()["turns"]
    assert [t["status"] for t in turns] == ["erased", "answered"]
    assert "deleted" in turns[0]["answer"] and turns[0]["passages"] == []
    assert "509" in turns[1]["answer"]
    db.expire_all()
    erased = db.scalars(select(AnswerRun).where(AnswerRun.redacted_reason.is_not(None))).all()
    assert len(erased) == 1 and erased[0].answer is None and erased[0].question == "q?"
