"""read only query role

Revision ID: 5fbfcb7b4f5d
Revises: 9c98af5a279c
Create Date: 2026-11-18 19:53:01.733603

"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401 - commonly needed in migrations
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5fbfcb7b4f5d"
down_revision: str | Sequence[str] | None = "9c98af5a279c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Role that answer queries run as: SELECT on datasets only. See app/query/executor.py.
    from app.query.executor import READER_GRANTS_SQL

    for statement in READER_GRANTS_SQL:
        op.execute(statement)


def downgrade() -> None:
    # Roles are shared by every database on the server (e.g. the test database), so the role
    # itself is kept; only this database's privileges are removed.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA datasets REVOKE SELECT ON TABLES FROM copilot_reader"
    )
    op.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA datasets FROM copilot_reader")
    op.execute("REVOKE USAGE ON SCHEMA datasets FROM copilot_reader")
