"""service keys: a key that acts for the people it names in X-Acting-User

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("is_service", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    op.drop_column("users", "is_service")
