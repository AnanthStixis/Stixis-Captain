"""Server-side HTTP API state shared by all API replicas (no prompt content).

Revision ID: 0007
Revises: 0006
"""

from alembic import op

from captain.db.schema import api_state

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    api_state.create(op.get_bind())


def downgrade() -> None:
    api_state.drop(op.get_bind())
