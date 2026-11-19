"""Run compiled queries with defense in depth.

Even if the compiler had a bug, a query runs:
- as the `copilot_reader` role, which can only SELECT from tables in the `datasets` schema
  (no access to users, sessions, grants or anything else);
- inside a READ ONLY transaction, so no statement can modify data;
- with a statement timeout, so a pathological query can't tie up the database.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text

from app.db import get_engine
from app.query.compiler import CompiledQuery

READER_ROLE = "copilot_reader"
STATEMENT_TIMEOUT_MS = 5000

# Applied by the migration, and again by tests after they recreate the `datasets` schema.
READER_GRANTS_SQL = [
    f"""DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
            CREATE ROLE {READER_ROLE} NOLOGIN;
          END IF;
        END $$""",
    f"GRANT USAGE ON SCHEMA datasets TO {READER_ROLE}",
    f"GRANT SELECT ON ALL TABLES IN SCHEMA datasets TO {READER_ROLE}",
    # Tables published later (by the application's own role) are readable automatically.
    f"ALTER DEFAULT PRIVILEGES IN SCHEMA datasets GRANT SELECT ON TABLES TO {READER_ROLE}",
]


class QueryTimeoutError(RuntimeError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, object]]


def _plain(value: object) -> object:
    """Convert database values to JSON-safe ones: decimals → float (rounding for display happens
    later), dates and timestamps → ISO text. Results are stored as JSON and sent to the browser."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def execute(compiled: CompiledQuery) -> QueryResult:
    return run_readonly(compiled.sql, compiled.params, compiled.columns)


def run_readonly(sql: str, params: dict, columns: list[str] | None = None) -> QueryResult:
    with get_engine().connect() as conn, conn.begin() as tx:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text(f"SET LOCAL ROLE {READER_ROLE}"))
        conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        try:
            result = conn.execute(text(sql), params)
        except Exception as e:
            if "statement timeout" in str(e):
                raise QueryTimeoutError("The query took too long and was stopped.") from e
            raise
        names = columns or list(result.keys())
        rows = [dict(zip(names, (_plain(v) for v in row), strict=True)) for row in result]
        tx.rollback()  # nothing to keep: read-only by design
    return QueryResult(names, rows)
