"""Following sources and in-app notifications (M7b): who is told, and what stays hidden when
access goes away. Leakage through notifications is also probed in test_leakage.py."""

import pytest
from sqlalchemy import func, select

from app.cli import publish_reference, seed_reference
from app.models import Notification, Subscription
from tests.conftest import login
from tests.test_ingestion_jobs import NEW_STORE, STORES_CSV, upload_stores, work

pytestmark = pytest.mark.integration

ADMIN, STEWARD = "admin@example.com", "steward@example.com"
OPS, FIN = "ops@example.com", "fin@example.com"
FOLLOW = "/api/sources/{}/subscription"


@pytest.fixture
def setup(db, make_user, ingest_queues):
    seed_reference(db)
    publish_reference(db)
    make_user(ADMIN, role="admin")
    make_user(STEWARD, role="steward", groups=("operations", "finance"))
    make_user(OPS, groups=("operations",))
    make_user(FIN, groups=("finance",))
    return db


def as_user(client, email):
    client.cookies.clear()
    client.headers["X-CSRF-Token"] = login(client, email)
    return client


def publish_stores(client, version: int) -> None:
    """The steward uploads and publishes a new version of `stores`."""
    as_user(client, STEWARD)
    extra = b"".join(
        f"S{90 + i},Store {i},Portugal,Southwest Europe,Retail,2026-06-01\n".encode()
        for i in range(version)
    )
    upload_stores(client, STORES_CSV + NEW_STORE + extra)
    work()
    resp = client.post(f"/api/steward/sources/stores/versions/{version}/publish")
    assert resp.status_code == 200, resp.text


def inbox(client, email) -> dict:
    return as_user(client, email).get("/api/notifications").json()


def grant(client, slug, groups) -> None:
    as_user(client, ADMIN)
    assert (
        client.put(f"/api/admin/sources/{slug}/grants", json={"groups": groups}).status_code == 200
    )


# ----------------------------------------------------------------------------- following


def test_you_can_follow_only_what_you_can_read(setup, client):
    as_user(client, OPS)
    assert client.put(FOLLOW.format("stores")).json() == {"source": "stores", "subscribed": True}
    assert client.put(FOLLOW.format("stores")).status_code == 200  # twice is still once
    listed = {s["slug"]: s["subscribed"] for s in client.get("/api/sources").json()}
    assert listed["stores"] is True and listed["transactions"] is False
    assert client.get("/api/sources/stores").json()["subscribed"] is True

    # A Finance-only source: the same 404 as a source that doesn't exist.
    assert client.put(FOLLOW.format("finance-margin-targets")).status_code == 404
    assert client.put(FOLLOW.format("no-such-source")).status_code == 404

    assert client.delete(FOLLOW.format("stores")).status_code == 204
    assert client.get("/api/sources/stores").json()["subscribed"] is False


# ----------------------------------------------------------------------------- publishing


def test_publishing_tells_followers_who_can_read_it_but_not_the_publisher(setup, client):
    for email in (OPS, STEWARD):
        as_user(client, email).put(FOLLOW.format("stores"))
    publish_stores(client, 2)

    got = inbox(client, OPS)
    assert got["unread"] == 1
    [note] = got["items"]
    assert (note["source"], note["kind"], note["version"], note["read"]) == (
        "stores",
        "published",
        2,
        False,
    )
    assert "version 2" in note["message"]
    assert inbox(client, STEWARD)["items"] == []  # they published it themselves
    assert inbox(client, FIN)["items"] == []  # not following

    marked = as_user(client, OPS).post("/api/notifications/read", json={}).json()
    assert marked["unread"] == 0 and marked["items"][0]["read"] is True


def test_marking_read_only_touches_your_own(setup, client):
    as_user(client, OPS).put(FOLLOW.format("stores"))
    publish_stores(client, 2)
    note_id = inbox(client, OPS)["items"][0]["id"]
    as_user(client, FIN).post("/api/notifications/read", json={"ids": [note_id]})
    assert inbox(client, OPS)["unread"] == 1


# ----------------------------------------------------------------------------- access goes away


def test_lost_access_hides_notifications_and_stops_new_ones(setup, client):
    db = setup
    as_user(client, OPS).put(FOLLOW.format("stores"))
    publish_stores(client, 2)
    assert inbox(client, OPS)["unread"] == 1

    grant(client, "stores", ["finance"])
    assert inbox(client, OPS) == {"unread": 0, "items": []}
    publish_stores(client, 3)  # while Olga can't read it: nothing is created for her
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Notification)) == 1

    grant(client, "stores", ["operations", "finance"])
    got = inbox(client, OPS)
    assert [n["version"] for n in got["items"]] == [2]


def test_archive_hides_and_delete_removes_notifications_and_followers(setup, client):
    db = setup
    as_user(client, OPS).put(FOLLOW.format("shipping-and-delivery"))
    as_user(client, OPS).put(FOLLOW.format("stores"))
    publish_stores(client, 2)

    as_user(client, STEWARD).post("/api/steward/sources/stores/archive")
    assert inbox(client, OPS)["items"] == []
    as_user(client, STEWARD).post("/api/steward/sources/stores/restore")
    assert len(inbox(client, OPS)["items"]) == 1

    as_user(client, ADMIN)
    resp = client.post(
        "/api/admin/sources/shipping-and-delivery/delete", json={"confirm": "shipping-and-delivery"}
    )
    assert resp.status_code == 200
    db.expire_all()
    followed = set(db.scalars(select(Subscription.source_id)))
    assert len(followed) == 1  # stores only
    assert inbox(client, OPS)["unread"] == 1
