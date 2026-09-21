"""provider keys set from the portal, encrypted at rest

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_keys",
        sa.Column("provider", sa.Text(), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("last4", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("provider_keys")
