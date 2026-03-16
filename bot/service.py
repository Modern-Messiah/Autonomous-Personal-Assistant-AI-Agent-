"""Application service for Telegram bot workflows."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.graph import run_search_graph
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.nodes.intent_node import IntentNode
from db import (
    get_active_search_criteria_record,
    replace_active_search_criteria,
    upsert_telegram_user,
)

SearchRunner = Callable[..., Awaitable[list[EnrichedApartment]]]


@dataclass(slots=True, frozen=True)
class SearchExecution:
    """Structured result returned by bot search service."""

    criteria: SearchCriteria
    apartments: list[EnrichedApartment]


class SearchBotService:
    """Coordinates persistence and graph execution for Telegram flows."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        intent_node: IntentNode | None = None,
        search_runner: SearchRunner = run_search_graph,
    ) -> None:
        self._session_factory = session_factory
        self._intent_node = intent_node or IntentNode()
        self._search_runner = search_runner

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

        async with self._session_factory() as session:
            user = await upsert_telegram_user(
                session,
                telegram_user_id=telegram_user_id,
                username=username,
            )
            await replace_active_search_criteria(
                session,
                user_id=user.id,
                criteria_payload=criteria.model_dump(mode="json"),
            )
            await session.commit()

        apartments = await self._search_runner(
            criteria,
            thread_id=f"telegram-user:{telegram_user_id}",
            checkpoint_ns="telegram-search",
        )
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
