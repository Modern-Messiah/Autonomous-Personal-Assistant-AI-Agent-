"""Database repositories for bot-facing workflows."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import Select, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from db.models import (
    ApartmentFeedbackRecord,
    ApartmentRecord,
    MonitorSettingsRecord,
    SearchCriteriaRecord,
    SeenApartment,
    User,
)

ApartmentDecision = Literal["saved", "rejected"]


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
    """Keep a single criteria row per user: drop the old one(s), persist the new."""
    await session.execute(
        delete(SearchCriteriaRecord).where(SearchCriteriaRecord.user_id == user_id)
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


async def get_monitor_target_by_telegram_user_id(
    session: AsyncSession,
    *,
    telegram_user_id: int,
) -> MonitorTarget | None:
    """Load active monitor target for one Telegram user if configured."""
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
            User.telegram_user_id == telegram_user_id,
            MonitorSettingsRecord.is_enabled.is_(True),
            SearchCriteriaRecord.is_active.is_(True),
        )
        .order_by(SearchCriteriaRecord.created_at.desc())
        .limit(1)
    )
    result = await session.execute(statement)
    row = result.first()
    if row is None:
        return None

    (
        user_id,
        resolved_telegram_user_id,
        username,
        criteria_payload,
        interval_minutes,
        last_checked_at,
    ) = row
    return MonitorTarget(
        user_id=user_id,
        telegram_user_id=resolved_telegram_user_id,
        username=username,
        criteria=SearchCriteria.model_validate(criteria_payload),
        interval_minutes=interval_minutes,
        last_checked_at=last_checked_at,
    )


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

    values_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for item in apartments:
        apartment = item.apartment
        key = (apartment.source, apartment.external_id)
        values_by_key[key] = {
            "external_id": apartment.external_id,
            "source": apartment.source,
            "url": apartment.url,
            "payload": item.model_dump(mode="json"),
        }

    # The table has two independent unique constraints. PostgreSQL can check
    # UNIQUE(url) before the (source, external_id) conflict arbiter, so two
    # identical speculative inserts may otherwise race on the secondary key.
    # Transaction-scoped locks serialize only the same authoritative identity.
    for source, external_id in sorted(values_by_key):
        lock_key = f"apartment:{source}:{external_id}"
        await session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )

    insert_statement = insert(ApartmentRecord).values(list(values_by_key.values()))
    statement = insert_statement.on_conflict_do_update(
        index_elements=[ApartmentRecord.source, ApartmentRecord.external_id],
        set_={
            "url": insert_statement.excluded.url,
            "payload": insert_statement.excluded.payload,
        },
    ).returning(ApartmentRecord)
    result = await session.execute(statement)
    records_by_key = {
        (record.source, record.external_id): record for record in result.scalars()
    }
    return [
        records_by_key[(item.apartment.source, item.apartment.external_id)]
        for item in apartments
    ]


async def list_apartment_records_by_urls(
    session: AsyncSession,
    *,
    urls: Sequence[str],
) -> list[ApartmentRecord]:
    """Load apartment records by URL while preserving the requested order."""
    unique_urls = list(dict.fromkeys(urls))
    if not unique_urls:
        return []

    result = await session.execute(
        select(ApartmentRecord).where(ApartmentRecord.url.in_(unique_urls))
    )
    records_by_url = {record.url: record for record in result.scalars()}
    return [records_by_url[url] for url in unique_urls if url in records_by_url]


async def list_apartment_records_by_external_ids(
    session: AsyncSession,
    *,
    external_ids: Sequence[str],
) -> list[ApartmentRecord]:
    """Load apartment records by external_id while preserving the requested order."""
    unique_ids = list(dict.fromkeys(external_ids))
    if not unique_ids:
        return []

    result = await session.execute(
        select(ApartmentRecord).where(ApartmentRecord.external_id.in_(unique_ids))
    )
    records_by_id = {record.external_id: record for record in result.scalars()}
    return [records_by_id[ext_id] for ext_id in unique_ids if ext_id in records_by_id]


async def upsert_apartment_feedback(
    session: AsyncSession,
    *,
    user_id: int,
    apartments: Sequence[ApartmentRecord],
    decision: ApartmentDecision,
) -> list[ApartmentFeedbackRecord]:
    """Persist one feedback decision for each apartment in the selection."""
    apartment_ids = [record.id for record in apartments]
    if not apartment_ids:
        return []

    decided_at = datetime.now(UTC)
    unique_ids = list(dict.fromkeys(apartment_ids))
    insert_statement = insert(ApartmentFeedbackRecord).values(
        [
            {
                "user_id": user_id,
                "apartment_id": apartment_id,
                "decision": decision,
                "decided_at": decided_at,
                "deleted_at": None,
            }
            for apartment_id in unique_ids
        ]
    )
    statement = insert_statement.on_conflict_do_update(
        index_elements=[
            ApartmentFeedbackRecord.user_id,
            ApartmentFeedbackRecord.apartment_id,
        ],
        set_={
            "decision": insert_statement.excluded.decision,
            "decided_at": insert_statement.excluded.decided_at,
            "deleted_at": None,
        },
    ).returning(ApartmentFeedbackRecord)
    result = await session.execute(statement)
    records_by_id = {record.apartment_id: record for record in result.scalars()}
    return [records_by_id[apartment.id] for apartment in apartments]


async def update_apartment_feedback_notion_sync(
    session: AsyncSession,
    *,
    user_id: int,
    synced_pages: Mapping[uuid.UUID, str],
    synced_at: datetime,
) -> list[ApartmentFeedbackRecord]:
    """Persist Notion sync metadata for already saved apartment feedback rows."""
    apartment_ids = list(synced_pages.keys())
    if not apartment_ids:
        return []

    result = await session.execute(
        select(ApartmentFeedbackRecord).where(
            ApartmentFeedbackRecord.user_id == user_id,
            ApartmentFeedbackRecord.apartment_id.in_(apartment_ids),
        )
    )
    feedback_records = list(result.scalars())
    for feedback_record in feedback_records:
        page_id = synced_pages.get(feedback_record.apartment_id)
        if page_id is None:
            continue
        feedback_record.notion_page_id = page_id
        feedback_record.notion_synced_at = synced_at

    await session.flush()
    return feedback_records


async def get_apartment_feedback_map(
    session: AsyncSession,
    *,
    user_id: int,
    apartments: Sequence[ApartmentRecord],
) -> dict[uuid.UUID, ApartmentDecision]:
    """Load persisted feedback decision by apartment id for one user."""
    apartment_ids = [record.id for record in apartments]
    if not apartment_ids:
        return {}

    result = await session.execute(
        select(
            ApartmentFeedbackRecord.apartment_id,
            ApartmentFeedbackRecord.decision,
        ).where(
            ApartmentFeedbackRecord.user_id == user_id,
            ApartmentFeedbackRecord.apartment_id.in_(apartment_ids),
        )
    )
    rows = [
        (cast(uuid.UUID, apartment_id), cast(ApartmentDecision, decision))
        for apartment_id, decision in result.all()
    ]
    return dict(rows)


async def mark_apartments_seen(
    session: AsyncSession,
    *,
    user_id: int,
    apartments: Sequence[ApartmentRecord],
) -> list[ApartmentRecord]:
    """Attach apartment records to a user without duplicating seen rows."""
    if not apartments:
        return []
    records_by_id = {apartment.id: apartment for apartment in apartments}
    unique_ids = list(records_by_id)
    statement = (
        insert(SeenApartment)
        .values(
            [
                {"user_id": user_id, "apartment_id": apartment_id}
                for apartment_id in unique_ids
            ]
        )
        .on_conflict_do_nothing(
            index_elements=[SeenApartment.user_id, SeenApartment.apartment_id]
        )
        .returning(SeenApartment.apartment_id)
    )
    result = await session.execute(statement)
    inserted_ids = set(result.scalars())
    return [
        records_by_id[apartment_id]
        for apartment_id in unique_ids
        if apartment_id in inserted_ids
    ]


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


async def list_feedback_apartments(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    decision: ApartmentDecision,
    limit: int = 10,
) -> list[EnrichedApartment]:
    """Return active (not soft-deleted) apartments for one feedback decision."""
    statement = (
        select(ApartmentRecord.payload)
        .join(ApartmentFeedbackRecord, ApartmentFeedbackRecord.apartment_id == ApartmentRecord.id)
        .join(User, ApartmentFeedbackRecord.user_id == User.id)
        .where(
            User.telegram_user_id == telegram_user_id,
            ApartmentFeedbackRecord.decision == decision,
            ApartmentFeedbackRecord.deleted_at.is_(None),
        )
        .order_by(ApartmentFeedbackRecord.decided_at.desc())
        .limit(limit)
    )
    result = await session.execute(statement)
    return [
        _load_enriched_apartment(payload)
        for payload in result.scalars()
    ]


async def list_trashed_apartments(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    decision: ApartmentDecision = "saved",
    limit: int = 10,
) -> list[EnrichedApartment]:
    """Return soft-deleted feedback apartments (the /trash list), newest first."""
    statement = (
        select(ApartmentRecord.payload)
        .join(ApartmentFeedbackRecord, ApartmentFeedbackRecord.apartment_id == ApartmentRecord.id)
        .join(User, ApartmentFeedbackRecord.user_id == User.id)
        .where(
            User.telegram_user_id == telegram_user_id,
            ApartmentFeedbackRecord.decision == decision,
            ApartmentFeedbackRecord.deleted_at.is_not(None),
        )
        .order_by(ApartmentFeedbackRecord.deleted_at.desc())
        .limit(limit)
    )
    result = await session.execute(statement)
    return [_load_enriched_apartment(payload) for payload in result.scalars()]


async def count_feedback_apartments(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    decision: ApartmentDecision,
) -> int:
    """Count active (not soft-deleted) apartments for one feedback decision."""
    statement = (
        select(func.count())
        .select_from(ApartmentFeedbackRecord)
        .join(User, ApartmentFeedbackRecord.user_id == User.id)
        .where(
            User.telegram_user_id == telegram_user_id,
            ApartmentFeedbackRecord.decision == decision,
            ApartmentFeedbackRecord.deleted_at.is_(None),
        )
    )
    return int((await session.execute(statement)).scalar_one())


async def delete_apartment_feedback(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    external_id: str,
    decision: ApartmentDecision = "saved",
) -> bool:
    """Soft-delete one feedback decision (recoverable via /trash); True if one changed."""
    statement = (
        select(ApartmentFeedbackRecord)
        .join(ApartmentRecord, ApartmentFeedbackRecord.apartment_id == ApartmentRecord.id)
        .join(User, ApartmentFeedbackRecord.user_id == User.id)
        .where(
            User.telegram_user_id == telegram_user_id,
            ApartmentRecord.external_id == external_id,
            ApartmentFeedbackRecord.decision == decision,
            ApartmentFeedbackRecord.deleted_at.is_(None),
        )
    )
    result = await session.execute(statement)
    records = list(result.scalars())
    deleted_at = datetime.now(UTC)
    for record in records:
        record.deleted_at = deleted_at
    return bool(records)


async def restore_apartment_feedback(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    external_id: str,
    decision: ApartmentDecision = "saved",
) -> bool:
    """Undo a soft delete, bringing a trashed feedback row back; True if one changed."""
    statement = (
        select(ApartmentFeedbackRecord)
        .join(ApartmentRecord, ApartmentFeedbackRecord.apartment_id == ApartmentRecord.id)
        .join(User, ApartmentFeedbackRecord.user_id == User.id)
        .where(
            User.telegram_user_id == telegram_user_id,
            ApartmentRecord.external_id == external_id,
            ApartmentFeedbackRecord.decision == decision,
            ApartmentFeedbackRecord.deleted_at.is_not(None),
        )
    )
    result = await session.execute(statement)
    records = list(result.scalars())
    for record in records:
        record.deleted_at = None
    return bool(records)


async def clear_apartment_feedback(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    external_id: str,
    decision: ApartmentDecision,
) -> bool:
    """Hard-delete an active feedback row (used to undo a rejection).

    Unlike a soft delete, this removes the row entirely, so the listing is no
    longer hidden from future searches (the search filter counts any feedback
    row regardless of ``deleted_at``) and no longer feeds the taste profile.
    Returns True if a row was removed.
    """
    statement = (
        select(ApartmentFeedbackRecord)
        .join(ApartmentRecord, ApartmentFeedbackRecord.apartment_id == ApartmentRecord.id)
        .join(User, ApartmentFeedbackRecord.user_id == User.id)
        .where(
            User.telegram_user_id == telegram_user_id,
            ApartmentRecord.external_id == external_id,
            ApartmentFeedbackRecord.decision == decision,
            ApartmentFeedbackRecord.deleted_at.is_(None),
        )
    )
    result = await session.execute(statement)
    records = list(result.scalars())
    for record in records:
        await session.delete(record)
    return bool(records)


async def purge_stale_records(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    seen_retention_days: int = 30,
    apartment_retention_days: int = 90,
) -> dict[str, int]:
    """Prune accumulated rows: inactive criteria, old seen links, stale apartments.

    Saved/rejected apartments are preserved (kept out of the apartment purge).
    """
    current = now or datetime.now(UTC)
    seen_cutoff = current - timedelta(days=seen_retention_days)
    apartment_cutoff = current - timedelta(days=apartment_retention_days)

    inactive_criteria = await session.execute(
        delete(SearchCriteriaRecord).where(SearchCriteriaRecord.is_active.is_(False))
    )
    old_seen = await session.execute(
        delete(SeenApartment).where(SeenApartment.first_seen_at < seen_cutoff)
    )
    old_apartments = await session.execute(
        delete(ApartmentRecord).where(
            ApartmentRecord.created_at < apartment_cutoff,
            ApartmentRecord.id.notin_(select(ApartmentFeedbackRecord.apartment_id)),
        )
    )
    return {
        "inactive_criteria": int(getattr(inactive_criteria, "rowcount", 0) or 0),
        "old_seen": int(getattr(old_seen, "rowcount", 0) or 0),
        "old_apartments": int(getattr(old_apartments, "rowcount", 0) or 0),
    }


def _load_enriched_apartment(payload: Mapping[str, Any]) -> EnrichedApartment:
    """Convert stored JSON payload into enriched apartment model."""
    if "apartment" in payload:
        return EnrichedApartment.model_validate(payload)
    return EnrichedApartment(apartment=Apartment.model_validate(payload))
