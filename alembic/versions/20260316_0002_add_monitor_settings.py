"""add monitor settings

Revision ID: 202603160002
Revises: 202603030001
Create Date: 2026-03-16 15:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202603160002"
down_revision: str | None = "202603030001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monitor_settings",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), server_default=sa.text("360"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        "idx_monitor_settings_is_enabled",
        "monitor_settings",
        ["is_enabled"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_monitor_settings_is_enabled", table_name="monitor_settings")
    op.drop_table("monitor_settings")
