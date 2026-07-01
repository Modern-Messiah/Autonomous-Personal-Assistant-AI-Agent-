"""Unit tests for the ARQ job adapter callables."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scheduler.jobs import process_monitor_target_job
from scheduler.service import SchedulerRunSummary


@pytest.mark.asyncio
async def test_process_monitor_target_job_parses_iso_and_serializes_summary() -> None:
    captured: dict[str, object] = {}

    class FakeService:
        async def process_monitor_target(
            self, *, telegram_user_id: int, checked_at: datetime
        ) -> SchedulerRunSummary:
            captured["telegram_user_id"] = telegram_user_id
            captured["checked_at"] = checked_at
            return SchedulerRunSummary(
                processed_users=1, notified_users=1, new_apartments=2, failed_users=0
            )

    ctx = {"scheduler_service": FakeService()}

    result = await process_monitor_target_job(ctx, 77, "2026-07-01T12:30:00+00:00")

    assert captured["telegram_user_id"] == 77
    assert captured["checked_at"] == datetime(2026, 7, 1, 12, 30, tzinfo=UTC)
    assert result == {
        "processed_users": 1,
        "notified_users": 1,
        "new_apartments": 2,
        "failed_users": 0,
    }


@pytest.mark.asyncio
async def test_process_monitor_target_job_propagates_failed_summary() -> None:
    class FailingSummaryService:
        async def process_monitor_target(
            self, *, telegram_user_id: int, checked_at: datetime
        ) -> SchedulerRunSummary:
            del telegram_user_id, checked_at
            return SchedulerRunSummary(processed_users=1, failed_users=1)

    result = await process_monitor_target_job(
        {"scheduler_service": FailingSummaryService()}, 5, "2026-07-01T00:00:00+00:00"
    )
    assert result == {
        "processed_users": 1,
        "notified_users": 0,
        "new_apartments": 0,
        "failed_users": 1,
    }
