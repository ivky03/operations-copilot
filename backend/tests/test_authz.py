"""Authorization: who can see which sources, and who can manage access.

Uses the real reference setup (groups, sources and grants from data/reference/access.yaml).
"""

import pytest
from sqlalchemy import select

from app.cli import seed_reference
from app.models import AuditLog, Source, SourceStatus
from tests.conftest import login

OPS = "ops.employee@brindlewood.example"
FIN = "finance.employee@brindlewood.example"
STEWARD = "data.steward@brindlewood.example"
ADMIN = "admin@brindlewood.example"


@pytest.fixture
def passwords(db):
    return seed_reference(db)


def slugs(client) -> set[str]:
    resp = client.get("/api/sources")
    assert resp.status_code == 200
    return {s["slug"] for s in resp.json()}


def test_operations_sees_shared_and_ops_sources_but_not_finance(client, passwords):
    login(client, OPS, passwords[OPS])
    visible = slugs(client)
    assert {"transactions", "cancellation-policy", "shipping-and-delivery"} <= visible
    assert not visible & {"finance-margin-targets", "month-end-close"}


def test_finance_sees_finance_sources_but_not_ops_only(client, passwords):
    login(client, FIN, passwords[FIN])
    visible = slugs(client)
    assert {"transactions", "finance-margin-targets", "month-end-close"} <= visible
    assert not visible & {"shipping-and-delivery", "store-operations-handbook"}


def test_restricted_source_looks_identical_to_a_missing_one(client, passwords):
    login(client, OPS, passwords[OPS])
    restricted = client.get("/api/sources/finance-margin-targets")
    missing = client.get("/api/sources/does-not-exist")
    assert restricted.status_code == missing.status_code == 404
    assert restricted.json() == missing.json()


def test_admin_has_no_implicit_data_access(client, passwords):
    login(client, ADMIN, passwords[ADMIN])
    assert slugs(client) == set()


def test_revoking_a_grant_takes_effect_on_the_next_request(passwords):
    from fastapi.testclient import TestClient

    from app.main import app

    ops, admin = TestClient(app), TestClient(app)
    login(ops, OPS, passwords[OPS])
    csrf = login(admin, ADMIN, passwords[ADMIN])
    assert "cancellation-policy" in slugs(ops)

    resp = admin.put(
        "/api/admin/sources/cancellation-policy/grants",
        json={"groups": ["finance"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert "cancellation-policy" not in slugs(ops)  # same session, no re-login needed
    assert ops.get("/api/sources/cancellation-policy").status_code == 404


def test_archived_source_is_hidden(client, passwords, db):
    source = db.scalar(select(Source).where(Source.slug == "cancellation-policy"))
    source.status = SourceStatus.ARCHIVED
    db.commit()
    login(client, OPS, passwords[OPS])
    assert "cancellation-policy" not in slugs(client)


@pytest.mark.parametrize("who", [OPS, STEWARD])
def test_non_admins_cannot_change_grants_including_stewards(client, passwords, who):
    csrf = login(client, who, passwords[who])
    resp = client.put(
        "/api/admin/sources/finance-margin-targets/grants",
        json={"groups": ["operations", "finance"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403
    assert client.get("/api/admin/users").status_code == 403


def test_admin_changes_are_audited(client, passwords, db):
    csrf = login(client, ADMIN, passwords[ADMIN])
    client.put(
        f"/api/admin/users/{OPS}/groups",
        json={"groups": ["operations", "finance"]},
        headers={"X-CSRF-Token": csrf},
    )
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "user.set_groups"))
    assert entry.target == OPS
    assert entry.details == {"before": "['operations']", "after": "['finance', 'operations']"}


def test_admin_can_deactivate_a_user_but_not_themselves(client, passwords):
    csrf = login(client, ADMIN, passwords[ADMIN])
    h = {"X-CSRF-Token": csrf}
    assert (
        client.patch(f"/api/admin/users/{OPS}", json={"is_active": False}, headers=h).status_code
        == 200
    )
    assert (
        client.patch(f"/api/admin/users/{ADMIN}", json={"is_active": False}, headers=h).status_code
        == 409
    )
