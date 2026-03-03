"""init schema

Revision ID: 202603030001
Revises:
Create Date: 2026-03-03 21:30:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "202603030001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id", name="uq_users_telegram_user_id"),
    )

    op.create_table(
        "search_criteria",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_search_criteria_user_active", "search_criteria", ["user_id", "is_active"], unique=False
    )

    op.create_table(
        "apartments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), server_default=sa.text("'krisha'"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id", name="uq_apartments_source_external_id"),
        sa.UniqueConstraint("url", name="uq_apartments_url"),
    )
    op.create_index(
        "idx_apartments_created_at",
        "apartments",
        [sa.text("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "seen_apartments",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("apartment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["apartment_id"], ["apartments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "apartment_id"),
    )
    op.create_index(
        "idx_seen_apartments_first_seen_at",
        "seen_apartments",
        [sa.text("first_seen_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_seen_apartments_first_seen_at", table_name="seen_apartments")
    op.drop_table("seen_apartments")

    op.drop_index("idx_apartments_created_at", table_name="apartments")
    op.drop_table("apartments")

    op.drop_index("idx_search_criteria_user_active", table_name="search_criteria")
    op.drop_table("search_criteria")

    op.drop_table("users")
