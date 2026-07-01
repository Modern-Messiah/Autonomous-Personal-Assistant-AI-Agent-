"""Monitor selection and processing against real PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from db.models import User
from db.repositories import (
    list_due_monitor_targets,
    replace_active_search_criteria,
    touch_monitor_last_checked_at,
    upsert_monitor_settings,
    upsert_telegram_user,
)
from scheduler.service import SchedulerService

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _criteria_payload(user_id: int) -> dict:
    return SearchCriteria(
        user_id=user_id,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        rooms=[2],
        page_limit=1,
    ).model_dump(mode="json")


def _apartment(external_id: str) -> EnrichedApartment:
    return EnrichedApartment(
        apartment=Apartment(
            external_id=external_id,
            source="krisha",
            url=f"https://krisha.kz/a/show/{external_id}",
            title="Monitor apartment",
            price_kzt=30_000_000,
            city="Almaty",
            rooms=2,
            area_m2=55,
            photos=[],
        )
    )


async def _make_monitor(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    is_enabled: bool = True,
    interval_minutes: int = 360,
    last_checked_at: datetime | None = None,
) -> User:
    user = await upsert_telegram_user(session, telegram_user_id=telegram_user_id, username=None)
    await replace_active_search_criteria(
        session, user_id=user.id, criteria_payload=_criteria_payload(telegram_user_id)
    )
    await upsert_monitor_settings(
        session, user_id=user.id, is_enabled=is_enabled, interval_minutes=interval_minutes
    )
    if last_checked_at is not None:
        await touch_monitor_last_checked_at(session, user_id=user.id, checked_at=last_checked_at)
    return user


@pytest.mark.asyncio
async def test_list_due_includes_never_checked_enabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _make_monitor(session, telegram_user_id=201)
        await session.commit()

    async with session_factory() as session:
        due = await list_due_monitor_targets(session, now=NOW)

    assert [target.telegram_user_id for target in due] == [201]
    assert due[0].criteria.city == "Almaty"
    assert due[0].interval_minutes == 360


@pytest.mark.asyncio
async def test_list_due_filters_disabled_and_not_yet_due_and_orders_nullsfirst(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _make_monitor(session, telegram_user_id=210, is_enabled=False)
        await _make_monitor(session, telegram_user_id=211, last_checked_at=NOW)
        await _make_monitor(session, telegram_user_id=212, last_checked_at=NOW - timedelta(hours=7))
        await _make_monitor(session, telegram_user_id=213)  # never checked
        await session.commit()

    async with session_factory() as session:
        due = await list_due_monitor_targets(session, now=NOW)
        limited = await list_due_monitor_targets(session, now=NOW, limit=1)

    ids = [target.telegram_user_id for target in due]
    assert 210 not in ids  # disabled monitor excluded
    assert 211 not in ids  # checked just now, interval not elapsed
    assert set(ids) == {212, 213}  # never-checked and elapsed are due
    assert ids[0] == 213  # NULLS FIRST: never-checked before elapsed
    assert [target.telegram_user_id for target in limited] == [213]  # limit respected


@pytest.mark.asyncio
async def test_process_monitor_target_notifies_only_new_apartments(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _make_monitor(session, telegram_user_id=220)
        await session.commit()

    notified: list[tuple[int, list[str]]] = []

    async def fake_notifier(
        telegram_user_id: int, criteria: SearchCriteria, apartments: list[EnrichedApartment]
    ) -> None:
        del criteria
        notified.append((telegram_user_id, [a.apartment.external_id for a in apartments]))

    async def fake_search(
        criteria: SearchCriteria, *, thread_id: str, checkpoint_ns: str
    ) -> list[EnrichedApartment]:
        del criteria, thread_id, checkpoint_ns
        return [_apartment("m-1")]

    service = SchedulerService(
        session_factory=session_factory,
        notifier=fake_notifier,
        search_runner=fake_search,
        now_provider=lambda: NOW,
    )

    first = await service.process_monitor_target(telegram_user_id=220, checked_at=NOW)
    assert first.processed_users == 1
    assert first.new_apartments == 1
    assert first.notified_users == 1
    assert notified == [(220, ["m-1"])]

    # Second run finds the same apartment, already seen -> no new, no notification.
    notified.clear()
    second = await service.process_monitor_target(
        telegram_user_id=220, checked_at=NOW + timedelta(hours=7)
    )
    assert second.processed_users == 1
    assert second.new_apartments == 0
    assert second.notified_users == 0
    assert notified == []


@pytest.mark.asyncio
async def test_process_monitor_target_no_config_is_noop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def fail_search(*args: object, **kwargs: object) -> list[EnrichedApartment]:
        raise AssertionError("search must not run without a monitor target")

    async def fail_notifier(*args: object, **kwargs: object) -> None:
        raise AssertionError("notifier must not run without a monitor target")

    service = SchedulerService(
        session_factory=session_factory,
        notifier=fail_notifier,
        search_runner=fail_search,
        now_provider=lambda: NOW,
    )
    summary = await service.process_monitor_target(telegram_user_id=999, checked_at=NOW)
    assert summary.processed_users == 0
    assert summary.new_apartments == 0
