"""Monitor settings service: per-user monitoring flags and intervals."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.monitoring import DEFAULT_MONITOR_INTERVAL_MINUTES
from db import (
    get_monitor_settings_record,
    upsert_monitor_settings,
    upsert_telegram_user,
)


@dataclass(slots=True, frozen=True)
class MonitorStatus:
    """Persistent monitoring settings exposed to bot handlers."""

    enabled: bool
    interval_minutes: int


class MonitorService:
    """Owns the /monitor feature: status, on/off flag, and check interval."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_monitor_status(
        self,
        *,
        telegram_user_id: int,
    ) -> MonitorStatus | None:
        """Return current monitoring settings for a Telegram user."""
        async with self._session_factory() as session:
            record = await get_monitor_settings_record(
                session,
                telegram_user_id=telegram_user_id,
            )
            if record is None:
                return None
            return MonitorStatus(
                enabled=record.is_enabled,
                interval_minutes=record.interval_minutes,
            )

    async def set_monitor_enabled(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        enabled: bool,
    ) -> MonitorStatus:
        """Create or update monitor flag for a Telegram user."""
        async with self._session_factory() as session:
            user = await upsert_telegram_user(
                session,
                telegram_user_id=telegram_user_id,
                username=username,
            )
            record = await upsert_monitor_settings(
                session,
                user_id=user.id,
                is_enabled=enabled,
            )
            await session.commit()
            return MonitorStatus(
                enabled=record.is_enabled,
                interval_minutes=record.interval_minutes,
            )

    async def set_monitor_interval(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        interval_minutes: int,
    ) -> MonitorStatus:
        """Create or update monitor interval for a Telegram user."""
        async with self._session_factory() as session:
            user = await upsert_telegram_user(
                session,
                telegram_user_id=telegram_user_id,
                username=username,
            )
            record = await upsert_monitor_settings(
                session,
                user_id=user.id,
                interval_minutes=interval_minutes,
            )
            await session.commit()
            return MonitorStatus(
                enabled=record.is_enabled,
                interval_minutes=record.interval_minutes,
            )

    def get_default_monitor_status(self) -> MonitorStatus:
        """Return default monitor settings when nothing is stored yet."""
        return MonitorStatus(
            enabled=False,
            interval_minutes=DEFAULT_MONITOR_INTERVAL_MINUTES,
        )
