"""Tests for scheduler runtime and monitor orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from db.repositories import MonitorTarget
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
