"""several keys per provider (a Jina key pool)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # provider was the primary key, so there could be exactly one key per provider.
    op.add_column(
        "provider_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
    )
    op.drop_constraint("provider_keys_pkey", "provider_keys", type_="primary")
    op.create_primary_key("provider_keys_pkey", "provider_keys", ["id"])
    op.create_index("ix_provider_keys_provider", "provider_keys", ["provider", "updated_at"])


def downgrade() -> None:
    # Back to one key per provider: keep the newest of each.
    op.execute("""
        DELETE FROM provider_keys a USING provider_keys b
        WHERE a.provider = b.provider
          AND (a.updated_at, a.id) < (b.updated_at, b.id)
    """)
    op.drop_index("ix_provider_keys_provider", table_name="provider_keys")
    op.drop_constraint("provider_keys_pkey", "provider_keys", type_="primary")
    op.create_primary_key("provider_keys_pkey", "provider_keys", ["provider"])
    op.drop_column("provider_keys", "id")
