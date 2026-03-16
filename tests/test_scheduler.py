"""Tests for scheduler runtime and monitor orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from db.repositories import MonitorTarget
from scheduler.producer import PROCESS_MONITOR_TARGET_JOB, SchedulerJobProducer
from scheduler.service import SchedulerService


class FakeSessionFactory:
    """Minimal async session factory for scheduler tests."""

    def __init__(self) -> None:
        self.session = FakeSession()

    def __call__(self) -> FakeSession:
        return self.session


class FakeSession:
    """Async context manager with commit tracking."""

    def __init__(self) -> None:
        self.commit_calls = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb

    async def commit(self) -> None:
        self.commit_calls += 1


def build_criteria() -> SearchCriteria:
    return SearchCriteria(
        user_id=77,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        max_price_kzt=40_000_000,
        rooms=[2],
        page_limit=2,
    )


def build_apartment(external_id: str) -> EnrichedApartment:
    return EnrichedApartment(
        apartment=Apartment(
            external_id=external_id,
            source="krisha",
            url=f"https://krisha.kz/a/show/{external_id}",
            title=f"Apartment {external_id}",
            price_kzt=30_000_000,
            city="Almaty",
            rooms=2,
            area_m2=55.0,
            photos=[f"https://photos.krisha.kz/{external_id}/1.jpg"],
        )
    )


@pytest.mark.asyncio
async def test_scheduler_notifies_only_new_apartments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()
    notifications: list[tuple[int, list[str]]] = []
    touched_users: list[int] = []
    marked_apartments: list[list[str]] = []
    now = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)

    service = SchedulerService(
        session_factory=session_factory,
        notifier=fake_notifier_factory(notifications),
        search_runner=fake_scheduler_search_runner,
        now_provider=lambda: now,
    )

    async def fake_list_due(session, *, now: datetime, limit: int):
        del session
        assert limit == 50
        assert now.tzinfo is not None
        return [
            MonitorTarget(
                user_id=1,
                telegram_user_id=77,
                username="tester",
                criteria=build_criteria(),
                interval_minutes=360,
                last_checked_at=None,
            )
        ]

    async def fake_upsert_records(session, *, apartments: list[EnrichedApartment]):
        del session
        assert len(apartments) == 2
        return [
            SimpleNamespace(id="record-1"),
            SimpleNamespace(id="record-2"),
        ]

    async def fake_get_unseen(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session
        assert user_id == 1
        return [apartments[1]]

    async def fake_mark_seen(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session
        marked_apartments.append([apartment.id for apartment in apartments])
        assert user_id == 1
        return apartments

    async def fake_touch(session, *, user_id: int, checked_at: datetime):
        del session
        assert checked_at == now
        touched_users.append(user_id)
        return SimpleNamespace(user_id=user_id)

    monkeypatch.setattr("scheduler.service.list_due_monitor_targets", fake_list_due)
    monkeypatch.setattr("scheduler.service.upsert_apartment_records", fake_upsert_records)
    monkeypatch.setattr("scheduler.service.get_unseen_apartment_records", fake_get_unseen)
    monkeypatch.setattr("scheduler.service.mark_apartments_seen", fake_mark_seen)
    monkeypatch.setattr("scheduler.service.touch_monitor_last_checked_at", fake_touch)

    summary = await service.run_pending_monitors()

    assert summary.processed_users == 1
    assert summary.notified_users == 1
    assert summary.new_apartments == 1
    assert summary.failed_users == 0
    assert notifications == [(77, ["900101"])]
    assert marked_apartments == [["record-2"]]
    assert touched_users == [1]
    assert session_factory.session.commit_calls == 1


@pytest.mark.asyncio
async def test_scheduler_returns_zero_summary_when_no_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()
    service = SchedulerService(
        session_factory=session_factory,
        notifier=fake_notifier_factory([]),
        search_runner=fake_scheduler_search_runner,
    )

    async def fake_list_due(session, *, now: datetime, limit: int):
        del session, now, limit
        return []

    monkeypatch.setattr("scheduler.service.list_due_monitor_targets", fake_list_due)

    summary = await service.run_pending_monitors()

    assert summary.processed_users == 0
    assert summary.notified_users == 0
    assert summary.new_apartments == 0
    assert summary.failed_users == 0
    assert session_factory.session.commit_calls == 0


@pytest.mark.asyncio
async def test_scheduler_does_not_mark_seen_when_notifier_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()
    touched_users: list[int] = []
    mark_calls = 0

    async def failing_notifier(
        telegram_user_id: int,
        criteria: SearchCriteria,
        apartments: list[EnrichedApartment],
    ) -> None:
        del telegram_user_id, criteria, apartments
        msg = "telegram unavailable"
        raise RuntimeError(msg)

    service = SchedulerService(
        session_factory=session_factory,
        notifier=failing_notifier,
        search_runner=fake_scheduler_search_runner,
    )

    async def fake_list_due(session, *, now: datetime, limit: int):
        del session, now, limit
        return [
            MonitorTarget(
                user_id=1,
                telegram_user_id=77,
                username="tester",
                criteria=build_criteria(),
                interval_minutes=360,
                last_checked_at=None,
            )
        ]

    async def fake_upsert_records(session, *, apartments: list[EnrichedApartment]):
        del session, apartments
        return [SimpleNamespace(id="record-1"), SimpleNamespace(id="record-2")]

    async def fake_get_unseen(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session, user_id
        return apartments

    async def fake_mark_seen(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session, user_id
        nonlocal mark_calls
        mark_calls += 1
        return apartments

    async def fake_touch(session, *, user_id: int, checked_at: datetime):
        del session, checked_at
        touched_users.append(user_id)
        return SimpleNamespace(user_id=user_id)

    monkeypatch.setattr("scheduler.service.list_due_monitor_targets", fake_list_due)
    monkeypatch.setattr("scheduler.service.upsert_apartment_records", fake_upsert_records)
    monkeypatch.setattr("scheduler.service.get_unseen_apartment_records", fake_get_unseen)
    monkeypatch.setattr("scheduler.service.mark_apartments_seen", fake_mark_seen)
    monkeypatch.setattr("scheduler.service.touch_monitor_last_checked_at", fake_touch)

    summary = await service.run_pending_monitors()

    assert summary.processed_users == 1
    assert summary.notified_users == 0
    assert summary.new_apartments == 0
    assert summary.failed_users == 1
    assert mark_calls == 0
    assert touched_users == []
    assert session_factory.session.commit_calls == 0


@pytest.mark.asyncio
async def test_scheduler_processes_one_monitor_target_by_telegram_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()
    now = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
    service = SchedulerService(
        session_factory=session_factory,
        notifier=fake_notifier_factory([]),
        search_runner=fake_scheduler_search_runner,
        now_provider=lambda: now,
    )

    async def fake_get_target(session, *, telegram_user_id: int):
        del session
        assert telegram_user_id == 77
        return MonitorTarget(
            user_id=1,
            telegram_user_id=77,
            username="tester",
            criteria=build_criteria(),
            interval_minutes=360,
            last_checked_at=None,
        )

    async def fake_process_target(*, target: MonitorTarget, checked_at: datetime | None = None):
        assert target.telegram_user_id == 77
        assert checked_at == now
        return SimpleNamespace(
            notified_users=1,
            new_apartments=2,
            failed_users=0,
        )

    monkeypatch.setattr(
        "scheduler.service.get_monitor_target_by_telegram_user_id",
        fake_get_target,
    )
    monkeypatch.setattr(service, "process_target", fake_process_target)

    summary = await service.process_monitor_target(
        telegram_user_id=77,
        checked_at=now,
    )

    assert summary.processed_users == 1
    assert summary.notified_users == 1
    assert summary.new_apartments == 2
    assert summary.failed_users == 0


@pytest.mark.asyncio
async def test_scheduler_returns_zero_when_single_target_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SchedulerService(
        session_factory=FakeSessionFactory(),
        notifier=fake_notifier_factory([]),
        search_runner=fake_scheduler_search_runner,
    )

    async def fake_get_target(session, *, telegram_user_id: int):
        del session, telegram_user_id
        return None

    monkeypatch.setattr(
        "scheduler.service.get_monitor_target_by_telegram_user_id",
        fake_get_target,
    )

    summary = await service.process_monitor_target(telegram_user_id=77)

    assert summary.processed_users == 0
    assert summary.notified_users == 0
    assert summary.new_apartments == 0
    assert summary.failed_users == 0


@pytest.mark.asyncio
async def test_scheduler_job_producer_enqueues_due_targets() -> None:
    now = datetime(2026, 3, 16, 16, 5, 42, tzinfo=UTC)
    enqueue_calls: list[tuple[str, tuple[object, ...], str | None, str | None]] = []

    class FakeQueue:
        async def enqueue_job(
            self,
            function: str,
            *args: object,
            _job_id: str | None = None,
            _queue_name: str | None = None,
        ) -> object | None:
            enqueue_calls.append((function, args, _job_id, _queue_name))
            if len(enqueue_calls) == 2:
                return None
            return object()

    class FakeService:
        def __init__(self) -> None:
            self.checked_at: datetime | None = None
            self.limit: int | None = None

        async def get_due_targets(
            self,
            *,
            limit: int | None = None,
            checked_at: datetime | None = None,
        ) -> list[MonitorTarget]:
            self.limit = limit
            self.checked_at = checked_at
            return [
                MonitorTarget(
                    user_id=1,
                    telegram_user_id=77,
                    username="tester",
                    criteria=build_criteria(),
                    interval_minutes=360,
                    last_checked_at=None,
                ),
                MonitorTarget(
                    user_id=2,
                    telegram_user_id=88,
                    username="tester2",
                    criteria=build_criteria(),
                    interval_minutes=360,
                    last_checked_at=None,
                ),
            ]

    service = FakeService()
    producer = SchedulerJobProducer(
        service=service,  # type: ignore[arg-type]
        queue=FakeQueue(),
        queue_name="krisha:monitor",
    )

    summary = await producer.enqueue_due_monitor_jobs(limit=20, checked_at=now)

    assert summary.due_users == 2
    assert summary.enqueued_jobs == 1
    assert summary.skipped_jobs == 1
    assert service.limit == 20
    assert service.checked_at == now
    assert enqueue_calls == [
        (
            PROCESS_MONITOR_TARGET_JOB,
            (77, now.isoformat()),
            "monitor:77:2026-03-16T16:05:00+00:00",
            "krisha:monitor",
        ),
        (
            PROCESS_MONITOR_TARGET_JOB,
            (88, now.isoformat()),
            "monitor:88:2026-03-16T16:05:00+00:00",
            "krisha:monitor",
        ),
    ]


def fake_notifier_factory(
    notifications: list[tuple[int, list[str]]],
):
    async def fake_notifier(
        telegram_user_id: int,
        criteria: SearchCriteria,
        apartments: list[EnrichedApartment],
    ) -> None:
        del criteria
        notifications.append(
            (
                telegram_user_id,
                [apartment.apartment.external_id for apartment in apartments],
            )
        )

    return fake_notifier


async def fake_scheduler_search_runner(
    criteria: SearchCriteria,
    *,
    thread_id: str,
    checkpoint_ns: str,
) -> list[EnrichedApartment]:
    assert criteria.city == "Almaty"
    assert thread_id == "telegram-monitor:77"
    assert checkpoint_ns == "telegram-monitor"
    return [build_apartment("900100"), build_apartment("900101")]
