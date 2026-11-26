"""Index the evidence ledger by time, so the dashboard's period queries do not scan it all.

A fresh database built from the current schema already has the index, so this is
"if not exists": it only adds it to databases created before it was defined.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_evidence_ledger_recorded_at ON evidence_ledger (recorded_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_evidence_ledger_recorded_at")
