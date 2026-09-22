"""job_domains.requeued_at: when a finished row was sent back for another try

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A retried row must not read the failed result it is retrying out of the shared
    # cache, which "anything finished after the job was created" would let it do.
    op.add_column("job_domains",
                  sa.Column("requeued_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("job_domains", "requeued_at")
