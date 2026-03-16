"""add apartment feedback

Revision ID: 202603160004
Revises: 202603160003
Create Date: 2026-03-16 19:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202603160004"
down_revision: str | None = "202603160003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apartment_feedback",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("apartment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "decision IN ('saved', 'rejected')",
            name="ck_apartment_feedback_decision",
        ),
        sa.ForeignKeyConstraint(["apartment_id"], ["apartments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "apartment_id"),
    )
    op.create_index(
        "idx_apartment_feedback_decision_decided_at",
        "apartment_feedback",
        ["decision", sa.text("decided_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_apartment_feedback_decision_decided_at",
        table_name="apartment_feedback",
    )
    op.drop_table("apartment_feedback")
