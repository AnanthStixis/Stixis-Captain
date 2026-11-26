"""Budget alert de-duplication shared by all instances (one alert per threshold per period).

Revision ID: 0006
Revises: 0005
"""

from alembic import op

from captain.db.schema import budget_alert_marks

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    budget_alert_marks.create(op.get_bind())


def downgrade() -> None:
    budget_alert_marks.drop(op.get_bind())
