"""Database repositories for bot-facing workflows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from db.models import (
    ApartmentRecord,
    MonitorSettingsRecord,
    SearchCriteriaRecord,
    SeenApartment,
    User,
)


@dataclass(slots=True, frozen=True)
class MonitorTarget:
    """Resolved monitor job for one user."""

    user_id: int
    telegram_user_id: int
    username: str | None
    criteria: SearchCriteria
    interval_minutes: int
    last_checked_at: datetime | None


async def upsert_telegram_user(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    username: str | None,
) -> User:
    """Insert or update Telegram user record."""
    result = await session.execute(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )
    user = result.scalar_one_or_none()
    normalized_username = username or None
    if user is None:
        user = User(telegram_user_id=telegram_user_id, username=normalized_username)
        session.add(user)
        await session.flush()
        return user

    if user.username != normalized_username:
        user.username = normalized_username
        await session.flush()
    return user


async def replace_active_search_criteria(
    session: AsyncSession,
    *,
    user_id: int,
    criteria_payload: Mapping[str, object],
) -> SearchCriteriaRecord:
    """Deactivate previous active criteria and persist the new active one."""
    await session.execute(
        update(SearchCriteriaRecord)
        .where(
            SearchCriteriaRecord.user_id == user_id,
            SearchCriteriaRecord.is_active.is_(True),
        )
        .values(is_active=False)
    )

    record = SearchCriteriaRecord(
        user_id=user_id,
        criteria=dict(criteria_payload),
        is_active=True,
    )
    session.add(record)
    await session.flush()
    return record


async def get_active_search_criteria_record(
    session: AsyncSession,
    *,
    telegram_user_id: int,
) -> SearchCriteriaRecord | None:
    """Load currently active criteria record for Telegram user."""
    statement: Select[tuple[SearchCriteriaRecord]] = (
        select(SearchCriteriaRecord)
        .join(User, SearchCriteriaRecord.user_id == User.id)
        .where(
            User.telegram_user_id == telegram_user_id,
            SearchCriteriaRecord.is_active.is_(True),
        )
        .order_by(SearchCriteriaRecord.created_at.desc())
        .limit(1)
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def upsert_monitor_settings(
    session: AsyncSession,
    *,
    user_id: int,
    is_enabled: bool | None = None,
    interval_minutes: int | None = None,
) -> MonitorSettingsRecord:
    """Create or update monitor settings for a user."""
    record = await session.get(MonitorSettingsRecord, user_id)
    if record is None:
        record = MonitorSettingsRecord(user_id=user_id)
        session.add(record)

    if is_enabled is not None:
        record.is_enabled = is_enabled
    if interval_minutes is not None:
        record.interval_minutes = interval_minutes
    record.updated_at = datetime.now(UTC)
    await session.flush()
    return record


async def get_monitor_settings_record(
    session: AsyncSession,
    *,
    telegram_user_id: int,
) -> MonitorSettingsRecord | None:
    """Load monitor settings for a Telegram user."""
    statement: Select[tuple[MonitorSettingsRecord]] = (
        select(MonitorSettingsRecord)
        .join(User, MonitorSettingsRecord.user_id == User.id)
        .where(User.telegram_user_id == telegram_user_id)
        .limit(1)
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def list_due_monitor_targets(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = 100,
) -> list[MonitorTarget]:
    """Return monitor targets that should be processed now."""
    statement = (
        select(
            User.id,
            User.telegram_user_id,
            User.username,
            SearchCriteriaRecord.criteria,
            MonitorSettingsRecord.interval_minutes,
            MonitorSettingsRecord.last_checked_at,
        )
        .join(MonitorSettingsRecord, MonitorSettingsRecord.user_id == User.id)
        .join(SearchCriteriaRecord, SearchCriteriaRecord.user_id == User.id)
        .where(
            MonitorSettingsRecord.is_enabled.is_(True),
            SearchCriteriaRecord.is_active.is_(True),
        )
        .order_by(MonitorSettingsRecord.last_checked_at.asc().nullsfirst(), User.id.asc())
    )
    result = await session.execute(statement)

    targets: list[MonitorTarget] = []
    for (
        user_id,
        telegram_user_id,
        username,
        criteria_payload,
        interval_minutes,
        last_checked_at,
    ) in result.all():
        if last_checked_at is not None:
            next_due_at = last_checked_at + timedelta(minutes=interval_minutes)
            if next_due_at > now:
                continue
        targets.append(
            MonitorTarget(
                user_id=user_id,
                telegram_user_id=telegram_user_id,
                username=username,
                criteria=SearchCriteria.model_validate(criteria_payload),
                interval_minutes=interval_minutes,
                last_checked_at=last_checked_at,
            )
        )
        if len(targets) >= limit:
            break
    return targets


async def touch_monitor_last_checked_at(
    session: AsyncSession,
    *,
    user_id: int,
    checked_at: datetime,
) -> MonitorSettingsRecord | None:
    """Persist the latest successful scheduler check time for a user."""
    record = await session.get(MonitorSettingsRecord, user_id)
    if record is None:
        return None
    record.last_checked_at = checked_at
    record.updated_at = checked_at
    await session.flush()
    return record


async def upsert_apartment_records(
    session: AsyncSession,
    *,
    apartments: Sequence[EnrichedApartment],
) -> list[ApartmentRecord]:
    """Insert new apartment records or refresh payload for existing ones."""
    if not apartments:
        return []

    lookup_keys = list(
        {
            (item.apartment.source, item.apartment.external_id)
            for item in apartments
        }
    )
    lookup_urls = list({item.apartment.url for item in apartments})

    existing_by_key: dict[tuple[str, str], ApartmentRecord] = {}
    if lookup_keys:
        result = await session.execute(
            select(ApartmentRecord).where(
                tuple_(ApartmentRecord.source, ApartmentRecord.external_id).in_(lookup_keys)
            )
        )
        existing_by_key = {
            (record.source, record.external_id): record
            for record in result.scalars()
        }

    existing_by_url: dict[str, ApartmentRecord] = {}
    if lookup_urls:
        result = await session.execute(
            select(ApartmentRecord).where(ApartmentRecord.url.in_(lookup_urls))
        )
        existing_by_url = {record.url: record for record in result.scalars()}

    records: list[ApartmentRecord] = []
    created_records = False
    for item in apartments:
        apartment = item.apartment
        key = (apartment.source, apartment.external_id)
        payload = item.model_dump(mode="json")
        record = existing_by_key.get(key) or existing_by_url.get(apartment.url)

        if record is None:
            record = ApartmentRecord(
                external_id=apartment.external_id,
                source=apartment.source,
                url=apartment.url,
                payload=payload,
            )
            session.add(record)
            created_records = True
        else:
            record.external_id = apartment.external_id
            record.source = apartment.source
            record.url = apartment.url
            record.payload = payload

        existing_by_key[key] = record
        existing_by_url[apartment.url] = record
        records.append(record)

    if created_records:
        await session.flush()

    return records


async def mark_apartments_seen(
    session: AsyncSession,
    *,
    user_id: int,
    apartments: Sequence[ApartmentRecord],
) -> list[ApartmentRecord]:
    """Attach apartment records to a user without duplicating seen rows."""
    unseen_apartments = await get_unseen_apartment_records(
        session,
        user_id=user_id,
        apartments=apartments,
    )
    if not unseen_apartments:
        return []

    for apartment in unseen_apartments:
        session.add(
            SeenApartment(
                user_id=user_id,
                apartment_id=apartment.id,
            )
        )

    await session.flush()
    return unseen_apartments


async def get_unseen_apartment_records(
    session: AsyncSession,
    *,
    user_id: int,
    apartments: Sequence[ApartmentRecord],
) -> list[ApartmentRecord]:
    """Return apartment records not yet linked to the user."""
    apartment_ids = [record.id for record in apartments]
    if not apartment_ids:
        return []

    result = await session.execute(
        select(SeenApartment.apartment_id).where(
            SeenApartment.user_id == user_id,
            SeenApartment.apartment_id.in_(apartment_ids),
        )
    )
    existing_ids = set(result.scalars())
    return [
        apartment
        for apartment in apartments
        if apartment.id not in existing_ids
    ]


async def list_seen_apartments(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    limit: int = 10,
) -> list[EnrichedApartment]:
    """Return most recently saved apartments for Telegram user."""
    statement = (
        select(ApartmentRecord.payload)
        .join(SeenApartment, SeenApartment.apartment_id == ApartmentRecord.id)
        .join(User, SeenApartment.user_id == User.id)
        .where(User.telegram_user_id == telegram_user_id)
        .order_by(SeenApartment.first_seen_at.desc())
        .limit(limit)
    )
    result = await session.execute(statement)
    return [
        _load_enriched_apartment(payload)
        for payload in result.scalars()
    ]


def _load_enriched_apartment(payload: Mapping[str, Any]) -> EnrichedApartment:
    """Convert stored JSON payload into enriched apartment model."""
    if "apartment" in payload:
        return EnrichedApartment.model_validate(payload)
    return EnrichedApartment(apartment=Apartment.model_validate(payload))
