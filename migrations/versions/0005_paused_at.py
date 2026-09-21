"""jobs.paused_at: when a job paused, so the page can say how long it has waited

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "paused_at")
