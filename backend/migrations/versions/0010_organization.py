"""Company profile: one organization row per install.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

from captain.db.schema import organization

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    organization.create(op.get_bind())


def downgrade() -> None:
    organization.drop(op.get_bind())
