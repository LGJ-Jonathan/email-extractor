"""multi-user: users, job ownership, Postgres work queue, domain locks

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("key_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "jobs",
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "jobs", sa.Column("fresh", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_index("ix_jobs_owner_id_created_at", "jobs", ["owner_id", "created_at"])

    op.create_table(
        "job_domains",
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("domain", sa.Text(), primary_key=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("from_cache", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("jina_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_job_domains_job_state_seq", "job_domains", ["job_id", "state", "seq"])
    op.create_index(
        "ix_job_domains_running",
        "job_domains",
        ["claimed_by"],
        postgresql_where=sa.text("state = 'running'"),
    )

    op.add_column("domains", sa.Column("lock_owner", sa.Text(), nullable=True))
    op.add_column("domains", sa.Column("lock_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("domains", "lock_until")
    op.drop_column("domains", "lock_owner")
    op.drop_index("ix_job_domains_running", table_name="job_domains")
    op.drop_index("ix_job_domains_job_state_seq", table_name="job_domains")
    op.drop_table("job_domains")
    op.drop_index("ix_jobs_owner_id_created_at", table_name="jobs")
    op.drop_column("jobs", "fresh")
    op.drop_column("jobs", "owner_id")
    op.drop_table("users")
