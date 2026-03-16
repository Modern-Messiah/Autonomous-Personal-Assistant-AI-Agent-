"""Application service for Telegram bot workflows."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.graph import run_search_graph
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.nodes.intent_node import IntentNode
from bot.monitoring import DEFAULT_MONITOR_INTERVAL_MINUTES
from db import (
    ApartmentDecision,
    get_active_search_criteria_record,
    get_apartment_feedback_map,
    get_monitor_settings_record,
    list_apartment_records_by_urls,
    list_feedback_apartments,
    mark_apartments_seen,
    replace_active_search_criteria,
    update_apartment_feedback_notion_sync,
    upsert_apartment_feedback,
    upsert_apartment_records,
    upsert_monitor_settings,
    upsert_telegram_user,
)

SearchRunner = Callable[..., Awaitable[list[EnrichedApartment]]]


class NotionApartmentSync(Protocol):
    """Minimal sync contract for pushing saved apartments to Notion."""

    async def sync_apartment(
        self,
        apartment: EnrichedApartment,
        *,
        page_id: str | None = None,
    ) -> str: ...


@dataclass(slots=True, frozen=True)
class SearchExecution:
    """Structured result returned by bot search service."""

    criteria: SearchCriteria
    apartments: list[EnrichedApartment]


@dataclass(slots=True, frozen=True)
class MonitorStatus:
    """Persistent monitoring settings exposed to bot handlers."""

    enabled: bool
    interval_minutes: int


class ActiveCriteriaNotFoundError(RuntimeError):
    """Raised when a refinement flow requires active criteria but none are stored."""


class SearchBotService:
    """Coordinates persistence and graph execution for Telegram flows."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        intent_node: IntentNode | None = None,
        search_runner: SearchRunner = run_search_graph,
        notion_sync: NotionApartmentSync | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._intent_node = intent_node or IntentNode()
        self._search_runner = search_runner
        self._notion_sync = notion_sync

    async def register_user(self, *, telegram_user_id: int, username: str | None) -> None:
        """Create or update user profile for Telegram user."""
        async with self._session_factory() as session:
            await upsert_telegram_user(
                session,
                telegram_user_id=telegram_user_id,
                username=username,
            )
            await session.commit()

    async def run_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        query: str,
    ) -> SearchExecution:
        """Parse criteria, persist them, and run the search graph."""
        criteria = self._intent_node.parse(user_id=telegram_user_id, message=query)
        return await self._persist_and_run_search(
            telegram_user_id=telegram_user_id,
            username=username,
            criteria=criteria,
        )

    async def refine_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        message: str,
    ) -> SearchExecution:
        """Merge refinement text into active criteria and rerun the search graph."""
        active_criteria = await self.get_active_criteria(telegram_user_id=telegram_user_id)
        if active_criteria is None:
            msg = "active criteria not found"
            raise ActiveCriteriaNotFoundError(msg)

        criteria = self._intent_node.refine(
            criteria=active_criteria,
            message=message,
        )
        return await self._persist_and_run_search(
            telegram_user_id=telegram_user_id,
            username=username,
            criteria=criteria,
        )

    async def _persist_and_run_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        criteria: SearchCriteria,
    ) -> SearchExecution:
        """Persist active criteria, execute search graph, and store results."""

        async with self._session_factory() as session:
            user = await upsert_telegram_user(
                session,
                telegram_user_id=telegram_user_id,
                username=username,
            )
            user_id = user.id
            await replace_active_search_criteria(
                session,
                user_id=user_id,
                criteria_payload=criteria.model_dump(mode="json"),
            )
            await session.commit()

        apartments = await self._search_runner(
            criteria,
            thread_id=f"telegram-user:{telegram_user_id}",
            checkpoint_ns="telegram-search",
        )
        if apartments:
            async with self._session_factory() as session:
                records = await upsert_apartment_records(
                    session,
                    apartments=apartments,
                )
                feedback_map = await get_apartment_feedback_map(
                    session,
                    user_id=user_id,
                    apartments=records,
                )
                await mark_apartments_seen(
                    session,
                    user_id=user_id,
                    apartments=records,
                )
                await session.commit()

            apartments = [
                apartment
                for apartment, record in zip(apartments, records, strict=True)
                if feedback_map.get(record.id) != "rejected"
            ]

        return SearchExecution(criteria=criteria, apartments=apartments)

    async def get_active_criteria(
        self,
        *,
        telegram_user_id: int,
    ) -> SearchCriteria | None:
        """Return current active criteria for user if present."""
        async with self._session_factory() as session:
            record = await get_active_search_criteria_record(
                session,
                telegram_user_id=telegram_user_id,
            )
            if record is None:
                return None
            return SearchCriteria.model_validate(record.criteria)

    async def get_saved_apartments(
        self,
        *,
        telegram_user_id: int,
        limit: int = 10,
    ) -> list[EnrichedApartment]:
        """Return recently saved apartments for one Telegram user."""
        async with self._session_factory() as session:
            return await list_feedback_apartments(
                session,
                telegram_user_id=telegram_user_id,
                decision="saved",
                limit=limit,
            )

    async def save_apartments(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        apartment_urls: list[str],
    ) -> int:
        """Persist a positive decision for the current apartment selection."""
        return await self._record_apartment_feedback(
            telegram_user_id=telegram_user_id,
            username=username,
            apartment_urls=apartment_urls,
            decision="saved",
        )

    async def reject_apartments(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        apartment_urls: list[str],
    ) -> int:
        """Persist a negative decision for the current apartment selection."""
        return await self._record_apartment_feedback(
            telegram_user_id=telegram_user_id,
            username=username,
            apartment_urls=apartment_urls,
            decision="rejected",
        )

    async def _record_apartment_feedback(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        apartment_urls: list[str],
        decision: ApartmentDecision,
    ) -> int:
        """Persist one user decision for the apartments currently in focus."""
        unique_urls = list(dict.fromkeys(apartment_urls))
        if not unique_urls:
            return 0

        user_id: int | None = None
        apartments_count = 0
        apartments_to_sync: list[tuple[uuid.UUID, EnrichedApartment, str | None]] = []

        async with self._session_factory() as session:
            user = await upsert_telegram_user(
                session,
                telegram_user_id=telegram_user_id,
                username=username,
            )
            user_id = user.id
            apartments = await list_apartment_records_by_urls(
                session,
                urls=unique_urls,
            )
            if not apartments:
                return 0
            apartments_count = len(apartments)
            feedback_records = await upsert_apartment_feedback(
                session,
                user_id=user.id,
                apartments=apartments,
                decision=decision,
            )
            if decision == "saved" and self._notion_sync is not None:
                feedback_by_apartment_id = {
                    record.apartment_id: record
                    for record in feedback_records
                }
                apartments_to_sync = [
                    (
                        apartment.id,
                        EnrichedApartment.model_validate(apartment.payload),
                        feedback_by_apartment_id[apartment.id].notion_page_id,
                    )
                    for apartment in apartments
                    if apartment.id in feedback_by_apartment_id
                ]
            await session.commit()

        if (
            decision == "saved"
            and self._notion_sync is not None
            and user_id is not None
            and apartments_to_sync
        ):
            synced_pages = await self._sync_saved_apartments_to_notion(
                apartments_to_sync=apartments_to_sync,
            )
            if synced_pages:
                async with self._session_factory() as session:
                    await update_apartment_feedback_notion_sync(
                        session,
                        user_id=user_id,
                        synced_pages=synced_pages,
                        synced_at=datetime.now(UTC),
                    )
                    await session.commit()

        return apartments_count

    async def _sync_saved_apartments_to_notion(
        self,
        *,
        apartments_to_sync: list[tuple[uuid.UUID, EnrichedApartment, str | None]],
    ) -> dict[uuid.UUID, str]:
        """Best-effort Notion sync for saved apartments."""
        if self._notion_sync is None:
            return {}

        synced_pages: dict[uuid.UUID, str] = {}
        for apartment_id, apartment, page_id in apartments_to_sync:
            try:
                synced_page_id = await self._notion_sync.sync_apartment(
                    apartment,
                    page_id=page_id,
                )
            except Exception:
                continue
            synced_pages[apartment_id] = synced_page_id
        return synced_pages

    async def get_monitor_status(
        self,
        *,
        telegram_user_id: int,
    ) -> MonitorStatus | None:
        """Return current monitoring settings for a Telegram user."""
        async with self._session_factory() as session:
            record = await get_monitor_settings_record(
                session,
                telegram_user_id=telegram_user_id,
            )
            if record is None:
                return None
            return MonitorStatus(
                enabled=record.is_enabled,
                interval_minutes=record.interval_minutes,
            )

    async def set_monitor_enabled(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        enabled: bool,
    ) -> MonitorStatus:
        """Create or update monitor flag for a Telegram user."""
        async with self._session_factory() as session:
            user = await upsert_telegram_user(
                session,
                telegram_user_id=telegram_user_id,
                username=username,
            )
            record = await upsert_monitor_settings(
                session,
                user_id=user.id,
                is_enabled=enabled,
            )
            await session.commit()
            return MonitorStatus(
                enabled=record.is_enabled,
                interval_minutes=record.interval_minutes,
            )

    async def set_monitor_interval(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        interval_minutes: int,
    ) -> MonitorStatus:
        """Create or update monitor interval for a Telegram user."""
        async with self._session_factory() as session:
            user = await upsert_telegram_user(
                session,
                telegram_user_id=telegram_user_id,
                username=username,
            )
            record = await upsert_monitor_settings(
                session,
                user_id=user.id,
                interval_minutes=interval_minutes,
            )
            await session.commit()
            return MonitorStatus(
                enabled=record.is_enabled,
                interval_minutes=record.interval_minutes,
            )

    def get_default_monitor_status(self) -> MonitorStatus:
        """Return default monitor settings when nothing is stored yet."""
        return MonitorStatus(
            enabled=False,
            interval_minutes=DEFAULT_MONITOR_INTERVAL_MINUTES,
        )
