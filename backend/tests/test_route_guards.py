"""Every API route is protected, including routes added in the future.

These tests discover routes from the app itself instead of listing them by hand, so a new
endpoint that forgets authentication fails CI automatically. Routes come from the OpenAPI
schema (FastAPI's official list of endpoints) rather than private router internals.
"""

import re

import pytest

from app.main import app
from tests.conftest import login

# The only routes reachable without logging in.
PUBLIC = {("GET", "/health"), ("POST", "/api/auth/login")}


def _api_routes() -> list[tuple[str, str]]:
    paths = app.openapi()["paths"]
    return sorted((method.upper(), path) for path, ops in paths.items() for method in ops)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "placeholder", path)


ALL_ROUTES = _api_routes()
PROTECTED = [r for r in ALL_ROUTES if r not in PUBLIC]
ADMIN_ROUTES = [r for r in PROTECTED if r[1].startswith("/api/admin")]


def test_public_allowlist_is_small_and_exact():
    assert set(PUBLIC) <= set(ALL_ROUTES), "allowlist names a route that doesn't exist"
    assert len(PROTECTED) >= 8


@pytest.mark.parametrize(("method", "path"), PROTECTED, ids=lambda x: str(x))
def test_every_protected_route_rejects_anonymous_requests(client, method, path):
    resp = client.request(method, _concrete(path), json={})
    assert resp.status_code == 401, f"{method} {path} answered {resp.status_code} without login"


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES, ids=lambda x: str(x))
def test_every_admin_route_rejects_non_admins(client, make_user, method, path):
    make_user("employee@example.com", groups=("operations",))
    csrf = login(client, "employee@example.com")
    resp = client.request(method, _concrete(path), json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403, f"{method} {path} answered {resp.status_code} for an employee"
