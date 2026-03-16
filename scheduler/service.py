"""Background monitoring service for scheduled search runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.graph import run_search_graph
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from db import (
    MonitorTarget,
    get_unseen_apartment_records,
    list_due_monitor_targets,
    mark_apartments_seen,
    touch_monitor_last_checked_at,
    upsert_apartment_records,
)

MonitorSearchRunner = Callable[..., Awaitable[list[EnrichedApartment]]]
MonitorNotifier = Callable[[int, SearchCriteria, list[EnrichedApartment]], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class SchedulerRunSummary:
    """Aggregated result of one scheduler polling cycle."""

    processed_users: int = 0
    notified_users: int = 0
    new_apartments: int = 0
    failed_users: int = 0


class SchedulerService:
    """Runs due monitor jobs and sends notifications for new apartments."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: MonitorNotifier,
        search_runner: MonitorSearchRunner = run_search_graph,
        now_provider: Callable[[], datetime] | None = None,
        batch_size: int = 50,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier
        self._search_runner = search_runner
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._batch_size = batch_size

    async def run_pending_monitors(
        self,
        *,
        limit: int | None = None,
    ) -> SchedulerRunSummary:
        """Process due monitor targets once."""
        now = self._now_provider()
        active_limit = limit or self._batch_size

        async with self._session_factory() as session:
            targets = await list_due_monitor_targets(
                session,
                now=now,
                limit=active_limit,
            )

        summary = SchedulerRunSummary()
        for target in targets:
            try:
                target_summary = await self._process_target(target=target, checked_at=now)
            except Exception:
                summary = SchedulerRunSummary(
                    processed_users=summary.processed_users + 1,
                    notified_users=summary.notified_users,
                    new_apartments=summary.new_apartments,
                    failed_users=summary.failed_users + 1,
                )
                continue

            summary = SchedulerRunSummary(
                processed_users=summary.processed_users + 1,
                notified_users=summary.notified_users + target_summary.notified_users,
                new_apartments=summary.new_apartments + target_summary.new_apartments,
                failed_users=summary.failed_users,
            )
        return summary

    async def _process_target(
        self,
        *,
        target: MonitorTarget,
        checked_at: datetime,
    ) -> SchedulerRunSummary:
        apartments = await self._search_runner(
            target.criteria,
            thread_id=f"telegram-monitor:{target.telegram_user_id}",
            checkpoint_ns="telegram-monitor",
        )

        async with self._session_factory() as session:
            records = await upsert_apartment_records(
                session,
                apartments=apartments,
            )
            unseen_records = await get_unseen_apartment_records(
                session,
                user_id=target.user_id,
                apartments=records,
            )
            unseen_ids = {record.id for record in unseen_records}
            new_apartments = [
                apartment
                for apartment, record in zip(apartments, records, strict=True)
                if record.id in unseen_ids
            ]

            if new_apartments:
                await self._notifier(
                    target.telegram_user_id,
                    target.criteria,
                    new_apartments,
                )
                await mark_apartments_seen(
                    session,
                    user_id=target.user_id,
                    apartments=unseen_records,
                )

            await touch_monitor_last_checked_at(
                session,
                user_id=target.user_id,
                checked_at=checked_at,
            )
            await session.commit()

        return SchedulerRunSummary(
            notified_users=1 if new_apartments else 0,
            new_apartments=len(new_apartments),
        )
