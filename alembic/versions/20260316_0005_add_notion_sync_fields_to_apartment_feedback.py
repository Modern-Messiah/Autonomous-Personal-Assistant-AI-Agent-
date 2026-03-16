"""add notion sync fields to apartment feedback

Revision ID: 202603160005
Revises: 202603160004
Create Date: 2026-03-16 20:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202603160005"
down_revision: str | None = "202603160004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "apartment_feedback",
        sa.Column("notion_page_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "apartment_feedback",
        sa.Column("notion_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("apartment_feedback", "notion_synced_at")
    op.drop_column("apartment_feedback", "notion_page_id")
