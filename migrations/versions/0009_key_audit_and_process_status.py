"""provider key audit trail, per-process key status, index for recent-rate queries

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_key_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),          # saved | removed
        sa.Column("last4", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_provider_key_audit_at", "provider_key_audit", ["at"])

    # What each process (api, worker) is actually using, written on every refresh,
    # so the portal can show a key that one process cannot decrypt.
    op.create_table(
        "process_status",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("status", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )

    # /estimate and /providers/status count domains finished recently across all jobs.
    op.create_index("ix_job_domains_done_finished_at", "job_domains", ["finished_at"],
                    postgresql_where=sa.text("state = 'done'"))


def downgrade() -> None:
    op.drop_index("ix_job_domains_done_finished_at", table_name="job_domains")
    op.drop_table("process_status")
    op.drop_index("ix_provider_key_audit_at", table_name="provider_key_audit")
    op.drop_table("provider_key_audit")
