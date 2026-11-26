"""Model/Tool Registry: providers, model aliases, routes, MCP servers, tools.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

from captain.db.schema import registry_items

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    registry_items.create(op.get_bind())


def downgrade() -> None:
    registry_items.drop(op.get_bind())
