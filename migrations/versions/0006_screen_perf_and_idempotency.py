"""job screens at 50k rows, active time, idempotent job creation

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

FLAGS = ("needs_review", "typesafe_called", "has_phone", "has_form", "has_linkedin")


def upgrade() -> None:
    # GET /jobs/{id}/recent: newest finished rows of one job. Without this it read and
    # sorted every done row of the job on each 3-5 s poll.
    op.create_index(
        "ix_job_domains_done_recent", "job_domains", ["job_id", sa.text("finished_at DESC")],
        postgresql_where=sa.text("state = 'done'"),
    )

    # job_counts read five fields out of every row's result jsonb on every poll;
    # the flags are now written once, at finish.
    # Spelled out (not looped) so tests/test_migration_matches_models.py can read them.
    op.add_column("job_domains", sa.Column("needs_review", sa.Boolean(), nullable=False,
                                           server_default=sa.false()))
    op.add_column("job_domains", sa.Column("typesafe_called", sa.Boolean(), nullable=False,
                                           server_default=sa.false()))
    op.add_column("job_domains", sa.Column("has_phone", sa.Boolean(), nullable=False,
                                           server_default=sa.false()))
    op.add_column("job_domains", sa.Column("has_form", sa.Boolean(), nullable=False,
                                           server_default=sa.false()))
    op.add_column("job_domains", sa.Column("has_linkedin", sa.Boolean(), nullable=False,
                                           server_default=sa.false()))
    op.execute("""
        UPDATE job_domains SET
            needs_review = coalesce((result->>'needs_review')::boolean, false),
            typesafe_called = coalesce((result->>'typesafe_called')::boolean, false),
            has_phone = jsonb_array_length(coalesce(result->'phones', '[]'::jsonb)) > 0,
            has_form = result->>'contact_form_url' IS NOT NULL,
            has_linkedin = result->'socials'->>'linkedin' IS NOT NULL
        WHERE result IS NOT NULL
    """)

    # Time spent working, for "time left" and "took": from the first claim, minus pauses.
    op.add_column("jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("paused_seconds", sa.Float(), nullable=False,
                                    server_default="0"))

    # A client retrying POST /jobs after a timeout gets the job it already created.
    op.add_column("jobs", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.create_index(
        "ux_jobs_owner_idempotency", "jobs",
        [sa.text("coalesce(owner_id::text, 'admin')"), "idempotency_key"],
        unique=True, postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_jobs_owner_idempotency", table_name="jobs")
    op.drop_column("jobs", "idempotency_key")
    op.drop_column("jobs", "paused_seconds")
    op.drop_column("jobs", "started_at")
    for flag in FLAGS:
        op.drop_column("job_domains", flag)
    op.drop_index("ix_job_domains_done_recent", table_name="job_domains")
