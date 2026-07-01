"""ARQ worker entrypoint (``WorkerSettings``) for per-user monitor jobs.

The job/lifecycle callables live in ``scheduler.jobs`` (import-time settings-free).
``WorkerSettings`` resolves configuration at class-definition time, which is fine
for the ``arq scheduler.arq_worker.WorkerSettings`` process entrypoint where the
environment is fully populated.
"""

from __future__ import annotations

from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from config.settings import get_settings
from scheduler.jobs import (
    parser_canary_cron,
    process_monitor_target_job,
    worker_shutdown,
    worker_startup,
)


def build_canary_cron_jobs() -> list[Any]:
    """Build the canary cron schedule from settings (empty when disabled)."""
    scheduler = get_settings().scheduler
    if not scheduler.canary_enabled:
        return []
    hours = set(range(0, 24, scheduler.canary_interval_hours))
    return [cron(parser_canary_cron, hour=hours, minute=0, run_at_startup=True)]


def build_worker_redis_settings() -> RedisSettings:
    """Build ARQ redis settings from project config."""
    settings = get_settings()
    password = (
        settings.redis.password.get_secret_value() if settings.redis.password is not None else None
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
    cron_jobs: ClassVar[list[Any]] = build_canary_cron_jobs()
    on_startup: ClassVar[Any] = worker_startup
    on_shutdown: ClassVar[Any] = worker_shutdown
    queue_name: ClassVar[str] = get_settings().arq.queue_name
    job_timeout: ClassVar[int] = get_settings().arq.job_timeout_seconds
    max_tries: ClassVar[int] = get_settings().arq.max_tries
    redis_settings: ClassVar[RedisSettings] = build_worker_redis_settings()
