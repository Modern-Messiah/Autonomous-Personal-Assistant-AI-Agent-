"""Database repositories for bot-facing workflows."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import SearchCriteriaRecord, User


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

