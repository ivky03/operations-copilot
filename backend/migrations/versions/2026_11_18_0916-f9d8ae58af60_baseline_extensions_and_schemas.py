"""baseline extensions and schemas

Revision ID: f9d8ae58af60
Revises:
Create Date: 2026-11-18 09:16:44.796685

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f9d8ae58af60"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector: embedding columns and similarity search (documents, schema index, values).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # pg_trgm: trigram fuzzy matching for value grounding ("lamp" ~ "BLUE DESK LAMP").
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # Steward-registered dataset versions live as typed tables in their own schema,
    # separate from application tables (users, chats, permissions).
    op.execute("CREATE SCHEMA IF NOT EXISTS datasets")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS datasets CASCADE")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
