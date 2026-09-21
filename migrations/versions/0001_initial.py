"""initial schema: jobs, job_items, domains, pages

Revision ID: 0001
Revises:
Create Date: 2026-09-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JOB_STATUSES = ("queued", "running", "paused", "done", "failed")
DOMAIN_STAGES = (
    "pending",
    "dns",
    "discovery",
    "fetching",
    "extracting",
    "judging",
    "done",
)


def upgrade() -> None:
    job_status = postgresql.ENUM(*JOB_STATUSES, name="job_status", create_type=False)
    domain_stage = postgresql.ENUM(*DOMAIN_STAGES, name="domain_stage", create_type=False)
    job_status.create(op.get_bind(), checkfirst=True)
    domain_stage.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", job_status, nullable=False, server_default="queued"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("columns", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("website_column", sa.Text(), nullable=True),
        sa.Column("pause_reason", sa.Text(), nullable=True),
    )

    op.create_table(
        "job_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("input_value", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("domain_source", sa.Text(), nullable=True),
        sa.Column("raw_row", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_job_items_job_id_row_index", "job_items", ["job_id", "row_index"])

    op.create_table(
        "domains",
        sa.Column("domain", sa.Text(), primary_key=True),
        sa.Column("stage", domain_stage, nullable=False, server_default="pending"),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_domains_finished_at", "domains", ["finished_at"])

    op.create_table(
        "pages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "domain",
            sa.Text(),
            sa.ForeignKey("domains.domain", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("jina_tokens", sa.Integer(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_pages_domain_fetched_at", "pages", ["domain", "fetched_at"])


def downgrade() -> None:
    op.drop_index("ix_pages_domain_fetched_at", table_name="pages")
    op.drop_table("pages")
    op.drop_index("ix_domains_finished_at", table_name="domains")
    op.drop_table("domains")
    op.drop_index("ix_job_items_job_id_row_index", table_name="job_items")
    op.drop_table("job_items")
    op.drop_table("jobs")
    postgresql.ENUM(name="domain_stage").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="job_status").drop(op.get_bind(), checkfirst=True)
