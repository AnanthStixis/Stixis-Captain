"""Persist budget overrides with the counters (shared by all instances, survive restarts).

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: a fresh database already has the column from revision 0001's tables.
    op.execute(
        "ALTER TABLE budget_buckets ADD COLUMN IF NOT EXISTS extra_micros BIGINT NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE budget_buckets DROP COLUMN IF EXISTS extra_micros")
