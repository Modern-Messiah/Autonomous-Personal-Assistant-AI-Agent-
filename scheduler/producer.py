"""ARQ producer helpers for monitor jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from scheduler.service import SchedulerService

PROCESS_MONITOR_TARGET_JOB = "process_monitor_target_job"


class ArqQueueProtocol(Protocol):
    """Minimal ARQ queue contract used by the producer."""

    async def enqueue_job(
        self,
        function: str,
        *args: object,
        _job_id: str | None = None,
        _queue_name: str | None = None,
    ) -> object | None: ...


@dataclass(slots=True, frozen=True)
class SchedulerEnqueueSummary:
    """Result of one enqueue cycle for due monitor jobs."""

    due_users: int = 0
    enqueued_jobs: int = 0
    skipped_jobs: int = 0


class SchedulerJobProducer:
    """Enqueue due user monitor jobs into an ARQ queue."""

    def __init__(
        self,
        *,
        service: SchedulerService,
        queue: ArqQueueProtocol,
        queue_name: str,
        job_name: str = PROCESS_MONITOR_TARGET_JOB,
    ) -> None:
        self._service = service
        self._queue = queue
        self._queue_name = queue_name
        self._job_name = job_name

    async def enqueue_due_monitor_jobs(
        self,
        *,
        limit: int | None = None,
        checked_at: datetime | None = None,
    ) -> SchedulerEnqueueSummary:
        """Load due users and enqueue one job per user."""
        active_checked_at = checked_at or datetime.now(UTC)
        targets = await self._service.get_due_targets(
            limit=limit,
            checked_at=active_checked_at,
        )

        summary = SchedulerEnqueueSummary(due_users=len(targets))
        for target in targets:
            result = await self._queue.enqueue_job(
                self._job_name,
                target.telegram_user_id,
                active_checked_at.isoformat(),
                _job_id=self._build_job_id(
                    telegram_user_id=target.telegram_user_id,
                    checked_at=active_checked_at,
                ),
                _queue_name=self._queue_name,
            )
            if result is None:
                summary = SchedulerEnqueueSummary(
                    due_users=summary.due_users,
                    enqueued_jobs=summary.enqueued_jobs,
                    skipped_jobs=summary.skipped_jobs + 1,
                )
                continue
            summary = SchedulerEnqueueSummary(
                due_users=summary.due_users,
                enqueued_jobs=summary.enqueued_jobs + 1,
                skipped_jobs=summary.skipped_jobs,
            )

        return summary

    @staticmethod
    def _build_job_id(*, telegram_user_id: int, checked_at: datetime) -> str:
        bucket = checked_at.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
        return f"monitor:{telegram_user_id}:{bucket}"
