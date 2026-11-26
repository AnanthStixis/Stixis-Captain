"""Control plane: client companies (Stixis install only; empty elsewhere).

Revision ID: 0012
Revises: 0011
"""

from alembic import op

from captain.db.schema import cp_companies

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cp_companies.create(op.get_bind())


def downgrade() -> None:
    cp_companies.drop(op.get_bind())
