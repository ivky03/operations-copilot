"""Health endpoint tests.

- The failure test patches the dependency checks, so it runs anywhere.
- The integration test needs `docker compose up` (Postgres + Floci) and a migrated database.
"""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app

client = TestClient(app)


def test_health_reports_503_when_a_dependency_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> dict[str, str]:
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(main, "check_database", broken)
    monkeypatch.setattr(main, "check_aws", lambda: {"status": "ok", "endpoint": "test"})

    resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == {"status": "error", "error": "ConnectionError"}
    # The raw error message is logged, not returned, so internals don't leak to clients.
    assert "unreachable" not in resp.text


@pytest.mark.integration
def test_health_ok_against_real_postgres_and_floci() -> None:
    resp = client.get("/health")

    assert resp.status_code == 200, resp.text
    checks = resp.json()["checks"]
    assert checks["database"]["status"] == "ok"
    assert checks["database"]["extensions"] == "pg_trgm,vector"  # baseline migration applied
    assert checks["aws"]["status"] == "ok"
