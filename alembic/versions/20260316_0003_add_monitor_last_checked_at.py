"""add monitor last checked at

Revision ID: 202603160003
Revises: 202603160002
Create Date: 2026-03-16 17:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202603160003"
down_revision: str | None = "202603160002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "monitor_settings",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("monitor_settings", "last_checked_at")
