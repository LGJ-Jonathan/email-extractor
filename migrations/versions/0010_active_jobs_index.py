"""index for the claim query's scan of active jobs

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The claim query and the completion sweep both start from jobs WHERE status IN
    # ('queued', 'running'); nothing is ever deleted, so without this the jobs table
    # is scanned end to end on every claim and every idle poll.
    op.create_index("ix_jobs_active", "jobs", ["status", "created_at"],
                    postgresql_where=sa.text("status IN ('queued', 'running', 'paused')"))


def downgrade() -> None:
    op.drop_index("ix_jobs_active", table_name="jobs")
