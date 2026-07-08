"""Unit tests for the ARQ job adapter callables."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import scheduler.jobs
from agent.tools.krisha_parser import ParserHealthReport
from scheduler.jobs import (
    parser_canary_cron,
    process_monitor_target_job,
    worker_shutdown,
    worker_startup,
)
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


@pytest.mark.asyncio
async def test_worker_startup_populates_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    observability_calls: list[bool] = []
    fake_bot = object()
    fake_service = object()

    monkeypatch.setattr(
        scheduler.jobs, "configure_observability", lambda: observability_calls.append(True)
    )
    monkeypatch.setattr(scheduler.jobs, "create_bot", lambda: fake_bot)
    monkeypatch.setattr(
        scheduler.jobs, "create_scheduler_service", lambda bot: fake_service
    )

    ctx: dict[str, Any] = {}
    await worker_startup(ctx)

    assert observability_calls == [True]
    assert ctx["bot"] is fake_bot
    assert ctx["scheduler_service"] is fake_service


@pytest.mark.asyncio
async def test_worker_shutdown_closes_bot_session_and_tolerates_missing_bot() -> None:
    closed: list[bool] = []

    async def close() -> None:
        closed.append(True)

    bot = SimpleNamespace(session=SimpleNamespace(close=close))
    await worker_shutdown({"bot": bot})
    assert closed == [True]

    # a worker that never finished startup has no bot — shutdown must not raise
    await worker_shutdown({})


@pytest.mark.asyncio
async def test_parser_canary_cron_maps_report(monkeypatch: pytest.MonkeyPatch) -> None:
    report = ParserHealthReport(
        ok=False,
        listing_count=3,
        previews_with_price=0,
        previews_with_specs=3,
        detail_checked=True,
        failures=["no preview carried a price (price parsing broke)"],
    )
    seen_bots: list[object] = []

    async def fake_run_parser_canary(*, bot: object) -> ParserHealthReport:
        seen_bots.append(bot)
        return report

    monkeypatch.setattr(scheduler.jobs, "run_parser_canary", fake_run_parser_canary)

    bot = object()
    result = await parser_canary_cron({"bot": bot})

    assert seen_bots == [bot]
    assert result == {
        "ok": False,
        "listing_count": 3,
        "failures": ["no preview carried a price (price parsing broke)"],
    }
