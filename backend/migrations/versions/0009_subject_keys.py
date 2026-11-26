"""Erasure keys for pseudonymised personal identifiers (ADR-005).

Revision ID: 0009
Revises: 0008
"""

from alembic import op

from captain.db.schema import subject_keys

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    subject_keys.create(op.get_bind())


def downgrade() -> None:
    subject_keys.drop(op.get_bind())
