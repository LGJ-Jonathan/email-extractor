"""review fixes: per-job result snapshots, cancelled status, job_items domain index

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # What each job actually received. Reading domains.result instead let a later
    # fresh job rewrite a finished job's CSV and counts after the fact.
    op.add_column("job_domains", sa.Column("status", sa.Text(), nullable=True))
    op.add_column(
        "job_domains",
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute("""
        UPDATE job_domains jd SET status = d.status, result = d.result
        FROM domains d WHERE d.domain = jd.domain AND jd.state = 'done'
    """)

    # Webhook payloads look rows up by (job_id, domain); without this every lookup
    # scanned the whole job.
    op.create_index("ix_job_items_job_id_domain", "job_items", ["job_id", "domain"])
    # Heartbeats renew a worker's locks every 20s; this keeps that off a full scan.
    op.create_index(
        "ix_domains_lock_owner", "domains", ["lock_owner"],
        postgresql_where=sa.text("lock_owner IS NOT NULL"),
    )

    # Postgres 12+ allows ADD VALUE in a transaction as long as the new value is not
    # used in the same one, so existing cancelled jobs keep failed + pause_reason.
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'cancelled'")

    # Jobs created before 0002 were handed to arq and have no queue rows, so they
    # could never complete. Nothing will ever run them now.
    op.execute("""
        UPDATE jobs SET status = 'failed', pause_reason = 'orphaned_by_queue_migration'
        WHERE status IN ('queued', 'running', 'paused')
          AND NOT EXISTS (SELECT 1 FROM job_domains jd WHERE jd.job_id = jobs.id)
          AND EXISTS (SELECT 1 FROM job_items ji WHERE ji.job_id = jobs.id
                      AND ji.domain IS NOT NULL)
    """)


def downgrade() -> None:
    op.drop_index("ix_domains_lock_owner", table_name="domains")
    op.drop_index("ix_job_items_job_id_domain", table_name="job_items")
    op.drop_column("job_domains", "result")
    op.drop_column("job_domains", "status")
    # Enum values cannot be dropped; 'cancelled' stays.
