"""add per-user feedback access index

Revision ID: 202607010007
Revises: 202606300006
Create Date: 2026-07-01 00:07:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607010007"
down_revision: str | None = "202606300006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "idx_apartment_feedback_decision_decided_at",
        table_name="apartment_feedback",
    )
    op.create_index(
        "idx_apartment_feedback_user_decision_deleted_decided",
        "apartment_feedback",
        ["user_id", "decision", "deleted_at", sa.text("decided_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_apartment_feedback_user_decision_deleted_decided",
        table_name="apartment_feedback",
    )
    op.create_index(
        "idx_apartment_feedback_decision_decided_at",
        "apartment_feedback",
        ["decision", sa.text("decided_at DESC")],
        unique=False,
    )
