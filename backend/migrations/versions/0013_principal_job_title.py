"""Users: job title (Developer, Manager, HR...), for people and reports, not access.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh database already has it (the first migration creates the current tables).
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("principals")}
    if "job_title" not in cols:
        op.add_column("principals", sa.Column("job_title", sa.String(128)))


def downgrade() -> None:
    op.drop_column("principals", "job_title")
