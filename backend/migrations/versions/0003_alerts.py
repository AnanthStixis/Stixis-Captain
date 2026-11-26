"""Persisted alerts (budget thresholds, overrides, downgrades, escalations, ledger lag).

Revision ID: 0003
Revises: 0002
"""

from alembic import op

from captain.db.schema import alerts

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    alerts.create(op.get_bind())


def downgrade() -> None:
    alerts.drop(op.get_bind())
