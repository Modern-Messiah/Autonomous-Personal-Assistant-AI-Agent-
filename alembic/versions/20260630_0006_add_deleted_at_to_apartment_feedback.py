"""add deleted_at (soft delete) to apartment feedback

Revision ID: 202606300006
Revises: 202603160005
Create Date: 2026-06-30 00:06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202606300006"
down_revision: str | None = "202603160005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "apartment_feedback",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("apartment_feedback", "deleted_at")
