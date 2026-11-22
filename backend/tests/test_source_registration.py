"""A brand-new source, end to end: an admin registers it and decides who can read it, a steward
uploads its first version, the worker prepares it, the steward publishes it, and only the
granted group sees it. Nothing about the source is hard-coded anywhere in the application."""

import pytest
import yaml
from sqlalchemy import select

from app.cli import seed_reference
from app.models import AuditLog, User
from app.query.catalog import Catalog
from tests.conftest import login
from tests.test_ingestion_jobs import work

pytestmark = pytest.mark.integration

ADMIN, STEWARD = "admin@example.com", "steward@example.com"
OPS, FIN = "ops@example.com", "fin@example.com"

MANIFEST = {
    "source": "suppliers",
    "title": "Suppliers",
    "description": "Companies Brindlewood Home buys products from.",
    "grain": "One row per supplier.",
    "primary_key": ["supplier_id"],
    "columns": [
        {"name": "supplier_id", "type": "string", "role": "id", "description": "Supplier ID."},
        {
            "name": "country",
            "type": "string",
            "role": "dimension",
            "description": "Supplier country.",
            "value_index": True,
        },
        {
            "name": "lead_time_days",
            "type": "integer",
            "role": "measure",
            "description": "Usual days from order to delivery.",
        },
    ],
    "metrics": [
        {
            "name": "supplier_count",
            "title": "Suppliers",
            "description": "Number of suppliers.",
            "aggregation": "count_distinct",
            "expression": "supplier_id",
        }
    ],
}
CSV = b"supplier_id,country,lead_time_days\nSU1,Portugal,21\nSU2,Poland,14\nSU3,Portugal,28\n"


@pytest.fixture
def people(db, make_user, ingest_queues):
    seed_reference(db)  # groups (operations, finance) and the reference sources
    make_user(ADMIN, role="admin")
    make_user(STEWARD, role="steward", groups=("operations",))
    make_user(OPS, groups=("operations",))
    make_user(FIN, groups=("finance",))


def as_user(client, email):
    client.cookies.clear()
    client.headers["X-CSRF-Token"] = login(client, email)
    return client


def register(client, **overrides):
    body = {"slug": "suppliers", "kind": "dataset", "title": "Suppliers", "groups": ["operations"]}
    return client.post("/api/admin/sources", json={**body, **overrides})


def test_a_new_dataset_goes_from_registration_to_answers(client, db, people):
    as_user(client, ADMIN)
    resp = register(client)
    assert resp.status_code == 201, resp.text
    assert resp.json() == {
        "slug": "suppliers",
        "kind": "dataset",
        "title": "Suppliers",
        "status": "active",
        "groups": ["operations"],
        "published_version": None,
    }
    assert db.scalar(
        select(AuditLog).where(AuditLog.action == "source.register", AuditLog.target == "suppliers")
    )

    as_user(client, STEWARD)
    files = {"manifest": ("m.yaml", yaml.safe_dump(MANIFEST).encode()), "data": ("s.csv", CSV)}
    assert client.post("/api/steward/sources/suppliers/uploads", files=files).status_code == 202
    work()
    assert client.post("/api/steward/sources/suppliers/versions/1/publish").status_code == 200

    as_user(client, OPS)
    assert client.get("/api/sources/suppliers").json()["published_version"] == 1
    ops = db.scalar(select(User).where(User.email == OPS))
    dataset, metric = Catalog.for_user(db, ops).find_metric("supplier_count")
    assert dataset.slug == "suppliers"

    as_user(client, FIN)  # not granted: absent, as if it didn't exist
    assert client.get("/api/sources/suppliers").status_code == 404
    fin = db.scalar(select(User).where(User.email == FIN))
    assert "supplier_count" not in Catalog.for_user(db, fin).metric_names()


def test_registration_rules(client, db, people):
    as_user(client, ADMIN)
    assert register(client).status_code == 201
    assert register(client).status_code == 409  # names are unique
    bad_dataset = register(client, slug="purchase-orders")
    assert bad_dataset.status_code == 422 and "underscores" in bad_dataset.json()["detail"]
    assert register(client, slug="supplier-code-of-conduct", kind="document").status_code == 201
    assert register(client, slug="x2", groups=["nobody"]).status_code == 422
    assert register(client, slug="x3", groups=[]).status_code == 422
    assert register(client, slug="Bad Name").status_code == 422

    listed = {s["slug"]: s for s in client.get("/api/admin/sources").json()}
    assert listed["supplier-code-of-conduct"]["kind"] == "document"
    assert listed["stores"]["published_version"] is None  # seeded, not published here
    assert [g["slug"] for g in client.get("/api/admin/groups").json()] == ["finance", "operations"]


def test_only_admins_register_sources(client, db, people):
    as_user(client, STEWARD)
    assert register(client).status_code == 403


def test_a_steward_outside_the_granted_groups_can_not_upload(client, db, make_user, people):
    as_user(client, ADMIN)
    register(client, groups=["finance"])
    as_user(client, STEWARD)  # operations only
    files = {"manifest": ("m.yaml", yaml.safe_dump(MANIFEST).encode()), "data": ("s.csv", CSV)}
    assert client.post("/api/steward/sources/suppliers/uploads", files=files).status_code == 404
