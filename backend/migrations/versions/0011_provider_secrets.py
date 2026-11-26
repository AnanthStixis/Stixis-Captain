"""Company AI keys, envelope-encrypted (plan step 3).

Revision ID: 0011
Revises: 0010
"""

from alembic import op

from captain.db.schema import provider_secrets

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    provider_secrets.create(op.get_bind())


def downgrade() -> None:
    provider_secrets.drop(op.get_bind())
