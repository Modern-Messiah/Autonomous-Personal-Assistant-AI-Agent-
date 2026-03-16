"""Scheduler runtime entrypoints."""

from __future__ import annotations

import asyncio
from importlib import import_module
from typing import Any

from aiogram import Bot

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from bot.app import create_bot
from config.settings import ArqSettings, get_settings
from db.session import get_session_factory
from scheduler.notifier import TelegramMonitorNotifier
from scheduler.producer import SchedulerEnqueueSummary, SchedulerJobProducer
from scheduler.service import SchedulerRunSummary, SchedulerService


async def noop_monitor_notifier(
    telegram_user_id: int,
    criteria: SearchCriteria,
    apartments: list[EnrichedApartment],
) -> None:
    """No-op notifier used by the ARQ producer path."""
    del telegram_user_id, criteria, apartments


def create_scheduler_service(bot: Bot | None = None) -> SchedulerService:
    """Build scheduler service with default runtime dependencies."""
    settings = get_settings()
    return SchedulerService(
        session_factory=get_session_factory(),
        notifier=(
            TelegramMonitorNotifier(bot)
            if bot is not None
            else noop_monitor_notifier
        ),
        batch_size=settings.scheduler.batch_size,
    )


def _build_arq_redis_settings(arq_settings: ArqSettings) -> Any:
    settings = get_settings()
    connections_module = import_module("arq.connections")
    redis_settings_cls = connections_module.RedisSettings
    password = (
        settings.redis.password.get_secret_value()
        if settings.redis.password is not None
        else None
    )
    return redis_settings_cls(
        host=settings.redis.host,
        port=settings.redis.port,
        database=settings.redis.db,
        password=password,
        default_queue_name=arq_settings.queue_name,
    )


async def create_arq_pool() -> Any:
    """Create ARQ redis pool lazily so local tests don't require arq installed."""
    connections_module = import_module("arq.connections")
    create_pool = connections_module.create_pool
    settings = get_settings()
    return await create_pool(_build_arq_redis_settings(settings.arq))


async def close_arq_pool(pool: Any) -> None:
    """Close ARQ redis pool when the runtime owns it."""
    if hasattr(pool, "aclose"):
        await pool.aclose()
        return
    if hasattr(pool, "close"):
        result = pool.close()
        if asyncio.iscoroutine(result):
            await result


async def run_scheduler_enqueue_once(
    *,
    service: SchedulerService | None = None,
    queue: Any | None = None,
) -> SchedulerEnqueueSummary:
    """Execute one ARQ enqueue cycle for due monitor jobs."""
    settings = get_settings()
    active_service = service or create_scheduler_service()
    owned_queue = queue is None
    active_queue = queue or await create_arq_pool()
    try:
        producer = SchedulerJobProducer(
            service=active_service,
            queue=active_queue,
            queue_name=settings.arq.queue_name,
        )
        return await producer.enqueue_due_monitor_jobs()
    finally:
        if owned_queue:
            await close_arq_pool(active_queue)


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
    if settings.scheduler.runtime == "arq":
        await run_scheduler_enqueue_forever(service=service)
        return

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


async def run_scheduler_enqueue_forever(
    service: SchedulerService | None = None,
    queue: Any | None = None,
) -> None:
    """Run ARQ producer loop with configured polling interval."""
    settings = get_settings()
    active_service = service or create_scheduler_service()
    owned_queue = queue is None
    active_queue = queue or await create_arq_pool()

    try:
        while True:
            producer = SchedulerJobProducer(
                service=active_service,
                queue=active_queue,
                queue_name=settings.arq.queue_name,
            )
            await producer.enqueue_due_monitor_jobs()
            await asyncio.sleep(settings.scheduler.poll_interval_seconds)
    finally:
        if owned_queue:
            await close_arq_pool(active_queue)


def main() -> None:
    """CLI entrypoint for `python -m scheduler`."""
    asyncio.run(run_scheduler_forever())
