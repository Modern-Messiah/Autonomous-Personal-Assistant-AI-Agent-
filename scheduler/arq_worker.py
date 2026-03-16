"""ARQ worker entrypoint for per-user monitor jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, cast

from arq.connections import RedisSettings

from bot.app import create_bot
from config.settings import get_settings
from scheduler.app import create_scheduler_service
from scheduler.service import SchedulerService


async def worker_startup(ctx: dict[str, Any]) -> None:
    """Initialize bot and scheduler service in ARQ worker context."""
    bot = create_bot()
    ctx["bot"] = bot
    ctx["scheduler_service"] = create_scheduler_service(bot)


async def worker_shutdown(ctx: dict[str, Any]) -> None:
    """Dispose ARQ worker resources."""
    bot = ctx.get("bot")
    if bot is None:
        return
    await bot.session.close()


async def process_monitor_target_job(
    ctx: dict[str, Any],
    telegram_user_id: int,
    checked_at_iso: str,
) -> dict[str, int]:
    """Process one queued monitor job for a Telegram user."""
    service = cast(SchedulerService, ctx["scheduler_service"])
    checked_at = datetime.fromisoformat(checked_at_iso)
    summary = await service.process_monitor_target(
        telegram_user_id=telegram_user_id,
        checked_at=checked_at,
    )
    return {
        "processed_users": summary.processed_users,
        "notified_users": summary.notified_users,
        "new_apartments": summary.new_apartments,
        "failed_users": summary.failed_users,
    }


def build_worker_redis_settings() -> RedisSettings:
    """Build ARQ redis settings from project config."""
    settings = get_settings()
    password = (
        settings.redis.password.get_secret_value()
        if settings.redis.password is not None
        else None
    )
    return RedisSettings(
        host=settings.redis.host,
        port=settings.redis.port,
        database=settings.redis.db,
        password=password,
    )


class WorkerSettings:
    """ARQ worker configuration for monitor processing jobs."""

    functions: ClassVar[list[Any]] = [process_monitor_target_job]
    on_startup: ClassVar[Any] = worker_startup
    on_shutdown: ClassVar[Any] = worker_shutdown
    queue_name: ClassVar[str] = get_settings().arq.queue_name
    job_timeout: ClassVar[int] = get_settings().arq.job_timeout_seconds
    max_tries: ClassVar[int] = get_settings().arq.max_tries
    redis_settings: ClassVar[RedisSettings] = build_worker_redis_settings()
