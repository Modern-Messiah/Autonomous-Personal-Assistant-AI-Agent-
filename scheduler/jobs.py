"""ARQ job and lifecycle callables.

Kept free of import-time settings resolution so the job functions can be imported
and unit-tested without a fully-populated environment. ``WorkerSettings`` (which
does resolve settings at class-definition time) lives in ``scheduler.arq_worker``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from bot.app import create_bot
from config.observability import configure_observability
from scheduler.app import create_scheduler_service
from scheduler.canary import run_parser_canary
from scheduler.service import SchedulerService


async def worker_startup(ctx: dict[str, Any]) -> None:
    """Initialize observability, bot, and scheduler service in ARQ worker context."""
    configure_observability()
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


async def parser_canary_cron(ctx: dict[str, Any]) -> dict[str, Any]:
    """Scheduled parser canary: verify krisha parsing and alert the admin on a break."""
    report = await run_parser_canary(bot=ctx.get("bot"))
    return {
        "ok": report.ok,
        "listing_count": report.listing_count,
        "failures": report.failures,
    }
