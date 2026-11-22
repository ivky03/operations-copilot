"""Shared test setup.

Tests run against a separate database, `<name>_test` (e.g. copilot_test), so they never
touch development data. It's created and migrated once per test run, and every table is
emptied after each test (including the `datasets` schema of published tables), so tests
can't affect each other.
"""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import get_settings
from app.query.executor import READER_GRANTS_SQL


def _clear_caches() -> None:
    from app import db

    get_settings.cache_clear()
    db.get_engine.cache_clear()
    db._session_factory.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def test_database() -> Iterator[str]:
    base_url = make_url(get_settings().database_url)
    test_url = base_url.set(database=f"{base_url.database}_test")

    # CREATE DATABASE can't run inside a transaction, hence AUTOCOMMIT.
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": test_url.database}
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{test_url.database}"'))
    admin.dispose()

    os.environ["DATABASE_URL"] = test_url.render_as_string(hide_password=False)
    os.environ["APP_ENV"] = "test"
    _clear_caches()
    command.upgrade(Config("alembic.ini"), "head")
    yield os.environ["DATABASE_URL"]
    _clear_caches()


@pytest.fixture(autouse=True)
def _clean_tables(test_database: str) -> Iterator[None]:
    yield
    from app.db import get_engine
    from app.models import Base

    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        # Published dataset versions are physical tables; emptying the version rows doesn't
        # drop them, so reset the whole schema too.
        conn.execute(text("DROP SCHEMA IF EXISTS datasets CASCADE"))
        conn.execute(text("CREATE SCHEMA datasets"))
        for statement in READER_GRANTS_SQL:  # recreating the schema drops its privileges
            conn.execute(text(statement))


@pytest.fixture
def db() -> Iterator[Session]:
    from app.db import _session_factory

    with _session_factory()() as session:
        yield session


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


PASSWORD = "correct horse battery staple"  # noqa: S105 - test-only password


@pytest.fixture
def make_user(db: Session):
    """Create a user with a role and groups (groups are created if missing)."""
    from app.models import Group, Role, User
    from app.security import hash_password

    def _make(email: str, role: str = "employee", groups: tuple[str, ...] = ()) -> User:
        group_rows = []
        for slug in groups:
            group = db.query(Group).filter_by(slug=slug).one_or_none()
            if group is None:
                group = Group(slug=slug, name=slug.title())
                db.add(group)
            group_rows.append(group)
        user = User(
            email=email,
            display_name=email.split("@")[0],
            password_hash=hash_password(PASSWORD),
            role=Role(role),
            groups=group_rows,
        )
        db.add(user)
        db.commit()
        return user

    return _make


def login(client: TestClient, email: str, password: str = PASSWORD) -> str:
    """Log in and return the CSRF token. The session cookie is stored on the client."""
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]


@pytest.fixture
def ingest_queues(monkeypatch):
    """Fresh, uniquely named queues for one test (deleted afterwards), short timeouts, and a
    test bucket. A running development worker listens on other queues, so it can't take
    these messages, and one test's leftover messages can't reach the next."""
    import uuid

    from app.aws import get_client
    from app.ingest import jobs

    suffix = uuid.uuid4().hex[:8]
    monkeypatch.setenv("UPLOAD_BUCKET", "opscopilot-uploads-test")
    monkeypatch.setenv("INGEST_QUEUE_NAME", f"opscopilot-ingest-test-{suffix}")
    monkeypatch.setenv("INGEST_DLQ_NAME", f"opscopilot-ingest-test-dlq-{suffix}")
    monkeypatch.setenv("INGEST_VISIBILITY_TIMEOUT", "3")
    get_settings.cache_clear()
    queues = jobs.ensure_infrastructure()
    yield queues
    sqs = get_client("sqs")
    for url in (queues.main, queues.dlq):
        sqs.delete_queue(QueueUrl=url)
    jobs._queues.clear()
    monkeypatch.undo()
    get_settings.cache_clear()
