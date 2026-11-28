"""schema entries: one embedded description per dataset version, for schema retrieval

Datasets published before this migration have no entry until `python -m app.cli reindex` runs.
Retrieval then can't find them (they are still described in "full" schema mode).

Revision ID: 6129a0c60c84
Revises: 2bcbe5f90699
Create Date: 2026-11-27 20:45:56.383414

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "6129a0c60c84"
down_revision: str | Sequence[str] | None = "2bcbe5f90699"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schema_entries",
        sa.Column("source_version_id", sa.UUID(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(dim=384), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_version_id"],
            ["source_versions.id"],
            name="schema_entries_source_version_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_version_id", name="schema_entries_pkey"),
    )


def downgrade() -> None:
    op.drop_table("schema_entries")
