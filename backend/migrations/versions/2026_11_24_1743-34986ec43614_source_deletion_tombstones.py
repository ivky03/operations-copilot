"""source deletion tombstones

Adds what deleting a source needs: the `deleted` version status (content removed, row kept),
when a source was deleted, and why a saved answer's content was erased.

Revision ID: 34986ec43614
Revises: bfda2476d98d
Create Date: 2026-11-24 17:43:32.911642

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "34986ec43614"
down_revision: str | Sequence[str] | None = "bfda2476d98d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # IF NOT EXISTS: Postgres can't remove an enum value, so the downgrade leaves it in place.
    op.execute("ALTER TYPE version_status ADD VALUE IF NOT EXISTS 'deleted'")
    op.add_column("answer_runs", sa.Column("redacted_reason", sa.String(length=30), nullable=True))
    op.add_column("sources", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("sources", "deleted_at")
    op.drop_column("answer_runs", "redacted_reason")
