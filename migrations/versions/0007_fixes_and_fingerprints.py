"""third review pass: backfill corrections, one user per email, idempotency fingerprints

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0006's backfill counted TypeSafe calls on cached rows (the old query excluded
    # them) and treated "" as a contact form / LinkedIn page (the app does not).
    op.execute("""
        UPDATE job_domains SET typesafe_called = false WHERE from_cache AND typesafe_called
    """)
    op.execute("""
        UPDATE job_domains SET
            has_form = nullif(result->>'contact_form_url', '') IS NOT NULL,
            has_linkedin = nullif(result->'socials'->>'linkedin', '') IS NOT NULL
        WHERE result IS NOT NULL
    """)

    # Acting users are matched on lower(name); make that the unique key so one email
    # can never become two identities.
    op.create_index("ux_users_lower_name", "users", [sa.text("lower(name)")], unique=True)

    # What the request that created a job looked like, so a reused Idempotency-Key with
    # a different upload is refused (409) instead of silently answered with the old job.
    op.add_column("jobs", sa.Column("idempotency_fingerprint", sa.Text(), nullable=True))

    # Cancelled jobs never got finished_at, so their active time grew forever.
    # Compared as text: on a fresh database 0003 adds 'cancelled' in this same
    # transaction, and Postgres refuses the new enum value as a literal until commit.
    op.execute("""
        UPDATE jobs SET finished_at = coalesce(paused_at, now())
        WHERE status::text = 'cancelled' AND finished_at IS NULL
    """)


def downgrade() -> None:
    op.drop_column("jobs", "idempotency_fingerprint")
    op.drop_index("ux_users_lower_name", table_name="users")
