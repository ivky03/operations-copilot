"""Database engine and connectivity check.

One SQLAlchemy Engine per process. It keeps a pool of open connections, so each request
doesn't pay the cost of opening a new TCP connection to Postgres.
"""

from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping: test a pooled connection before using it, so a Postgres restart
    # doesn't surface as a confusing error on the first request afterwards.
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def check_database() -> dict[str, str]:
    """Return database status, plus the extensions we depend on, for the health endpoint."""
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
        rows = conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')")
        ).scalars()
        extensions = sorted(rows)
    return {"status": "ok", "extensions": ",".join(extensions) or "none"}
