"""Scheduler runtime entrypoints."""

from __future__ import annotations

import asyncio

from aiogram import Bot

from bot.app import create_bot
from config.settings import get_settings
from db.session import get_session_factory
from scheduler.notifier import TelegramMonitorNotifier
from scheduler.service import SchedulerRunSummary, SchedulerService


def create_scheduler_service(bot: Bot) -> SchedulerService:
    """Build scheduler service with default runtime dependencies."""
    settings = get_settings()
    return SchedulerService(
        session_factory=get_session_factory(),
        notifier=TelegramMonitorNotifier(bot),
        batch_size=settings.scheduler.batch_size,
    )


async def run_scheduler_once(service: SchedulerService | None = None) -> SchedulerRunSummary:
    """Execute one scheduler polling cycle."""
    if service is not None:
        return await service.run_pending_monitors()

    bot = create_bot()
    try:
        return await create_scheduler_service(bot).run_pending_monitors()
    finally:
        await bot.session.close()


async def run_scheduler_forever(service: SchedulerService | None = None) -> None:
    """Run scheduler loop with configured polling interval."""
    settings = get_settings()
    if service is not None:
        while True:
            await service.run_pending_monitors()
            await asyncio.sleep(settings.scheduler.poll_interval_seconds)

    bot = create_bot()
    try:
        active_service = create_scheduler_service(bot)
        while True:
            await active_service.run_pending_monitors()
            await asyncio.sleep(settings.scheduler.poll_interval_seconds)
    finally:
        await bot.session.close()


def main() -> None:
    """CLI entrypoint for `python -m scheduler`."""
    asyncio.run(run_scheduler_forever())
