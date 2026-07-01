"""Repository behavior against real PostgreSQL."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.models.apartment import Apartment
from agent.models.enriched import EnrichedApartment
from db.models import ApartmentRecord, SeenApartment
from db.repositories import (
    clear_apartment_feedback,
    delete_apartment_feedback,
    list_feedback_apartments,
    list_trashed_apartments,
    mark_apartments_seen,
    restore_apartment_feedback,
    upsert_apartment_feedback,
    upsert_apartment_records,
    upsert_telegram_user,
)

pytestmark = pytest.mark.integration


def apartment(external_id: str = "apt-1") -> EnrichedApartment:
    return EnrichedApartment(
        apartment=Apartment(
            external_id=external_id,
            source="krisha",
            url=f"https://krisha.kz/a/show/{external_id}",
            title="Integration apartment",
            price_kzt=30_000_000,
            city="Almaty",
            rooms=2,
            area_m2=55,
            photos=[],
        )
    )


@pytest.mark.asyncio
async def test_concurrent_apartment_upsert_creates_one_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def write() -> None:
        async with session_factory() as session:
            await upsert_apartment_records(session, apartments=[apartment()])
            await session.commit()

    await asyncio.gather(write(), write())

    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ApartmentRecord))
        ) == 1


@pytest.mark.asyncio
async def test_feedback_soft_delete_and_restore_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        user = await upsert_telegram_user(
            session, telegram_user_id=1001, username="integration"
        )
        record = (await upsert_apartment_records(session, apartments=[apartment()]))[0]
        await upsert_apartment_feedback(
            session, user_id=user.id, apartments=[record], decision="saved"
        )
        await session.commit()

    async with session_factory() as session:
        assert len(
            await list_feedback_apartments(
                session, telegram_user_id=1001, decision="saved"
            )
        ) == 1
        assert await delete_apartment_feedback(
            session, telegram_user_id=1001, external_id="apt-1"
        )
        await session.commit()

    async with session_factory() as session:
        assert await list_feedback_apartments(
            session, telegram_user_id=1001, decision="saved"
        ) == []
        assert len(await list_trashed_apartments(session, telegram_user_id=1001)) == 1
        assert await restore_apartment_feedback(
            session, telegram_user_id=1001, external_id="apt-1"
        )
        await session.commit()

    async with session_factory() as session:
        assert len(
            await list_feedback_apartments(
                session, telegram_user_id=1001, decision="saved"
            )
        ) == 1


@pytest.mark.asyncio
async def test_clear_rejected_feedback_removes_it_from_search_and_trash(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        user = await upsert_telegram_user(
            session, telegram_user_id=1003, username="integration"
        )
        record = (await upsert_apartment_records(session, apartments=[apartment()]))[0]
        await upsert_apartment_feedback(
            session, user_id=user.id, apartments=[record], decision="rejected"
        )
        await session.commit()

    async with session_factory() as session:
        # A rejected item is active feedback (hidden from search) and shows in trash.
        assert len(
            await list_feedback_apartments(
                session, telegram_user_id=1003, decision="rejected"
            )
        ) == 1
        assert await clear_apartment_feedback(
            session, telegram_user_id=1003, external_id="apt-1", decision="rejected"
        )
        await session.commit()

    async with session_factory() as session:
        # Un-rejected: no feedback row remains, so it can resurface in search.
        assert await list_feedback_apartments(
            session, telegram_user_id=1003, decision="rejected"
        ) == []
        assert not await clear_apartment_feedback(
            session, telegram_user_id=1003, external_id="apt-1", decision="rejected"
        )


@pytest.mark.asyncio
async def test_concurrent_seen_insert_reports_only_one_new_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        user = await upsert_telegram_user(session, telegram_user_id=1002, username=None)
        record = (await upsert_apartment_records(session, apartments=[apartment()]))[0]
        user_id = user.id
        await session.commit()

    async def mark() -> int:
        async with session_factory() as session:
            result = await mark_apartments_seen(
                session, user_id=user_id, apartments=[record]
            )
            await session.commit()
            return len(result)

    assert sorted(await asyncio.gather(mark(), mark())) == [0, 1]
    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(SeenApartment))
        ) == 1
