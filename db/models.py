"""Database models for core entities."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class User(Base):
    """Telegram user."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    search_criteria: Mapped[list["SearchCriteriaRecord"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    monitor_settings: Mapped["MonitorSettingsRecord | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    seen_apartments: Mapped[list["SeenApartment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class SearchCriteriaRecord(Base):
    """Stored search criteria for a user."""

    __tablename__ = "search_criteria"
    __table_args__ = (Index("idx_search_criteria_user_active", "user_id", "is_active"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    criteria: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="search_criteria")


class ApartmentRecord(Base):
    """Apartment listing data."""

    __tablename__ = "apartments"
    __table_args__ = (
        UniqueConstraint(
            "source", "external_id", name="uq_apartments_source_external_id"
        ),
        UniqueConstraint("url", name="uq_apartments_url"),
        Index("idx_apartments_created_at", desc("created_at")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        Text, nullable=False, default="krisha", server_default=text("'krisha'")
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    seen_by_users: Mapped[list["SeenApartment"]] = relationship(
        back_populates="apartment", cascade="all, delete-orphan"
    )


class MonitorSettingsRecord(Base):
    """Per-user monitoring configuration for background checks."""

    __tablename__ = "monitor_settings"
    __table_args__ = (Index("idx_monitor_settings_is_enabled", "is_enabled"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=360, server_default=text("360")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship(back_populates="monitor_settings")


class SeenApartment(Base):
    """Many-to-many table for user/listing deduplication."""

    __tablename__ = "seen_apartments"
    __table_args__ = (Index("idx_seen_apartments_first_seen_at", desc("first_seen_at")),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    apartment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apartments.id", ondelete="CASCADE"), primary_key=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="seen_apartments")
    apartment: Mapped["ApartmentRecord"] = relationship(back_populates="seen_by_users")
