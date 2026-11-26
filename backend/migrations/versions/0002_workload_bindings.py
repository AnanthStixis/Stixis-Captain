"""Workload identity bindings (OIDC issuer + subject -> Captain principal/context).

Revision ID: 0002
Revises: 0001
"""

from alembic import op

from captain.db.schema import workload_bindings

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    workload_bindings.create(op.get_bind())


def downgrade() -> None:
    workload_bindings.drop(op.get_bind())
