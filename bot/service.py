"""Application service for Telegram bot workflows."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.graph import run_search_graph
from agent.locations import LOCATIONS
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.nodes.intent_node import IntentNode
from agent.tools.krisha_parser import AntiBotBlockedError
from bot.errors import (
    SEARCH_BLOCKED_MESSAGE,
    SEARCH_EXECUTION_ERROR_MESSAGE,
    ActiveCriteriaNotFoundError,
    CriteriaUnchangedError,
    NoPreferencesError,
    SearchExecutionError,
)
from bot.feedback_service import FeedbackService, NotionApartmentSync, RestoreOutcome
from bot.monitor_service import MonitorService, MonitorStatus
from bot.recommendation_service import (
    Recommendation,
    RecommendationResult,
    RecommendationService,
)
from db import (
    get_active_search_criteria_record,
    get_apartment_feedback_map,
    get_async_postgres_checkpointer,
    mark_apartments_seen,
    replace_active_search_criteria,
    upsert_apartment_records,
    upsert_telegram_user,
)

__all__ = [
    "SEARCH_BLOCKED_MESSAGE",
    "SEARCH_EXECUTION_ERROR_MESSAGE",
    "ActiveCriteriaNotFoundError",
    "CriteriaUnchangedError",
    "MonitorStatus",
    "NoPreferencesError",
    "NotionApartmentSync",
    "Recommendation",
    "RecommendationResult",
    "RestoreOutcome",
    "SearchBotService",
    "SearchExecution",
    "SearchExecutionError",
    "SearchRunner",
    "run_search_graph_with_postgres",
]

SearchRunner = Callable[..., Awaitable[list[EnrichedApartment]]]


async def run_search_graph_with_postgres(
    criteria: SearchCriteria, **kwargs: Any
) -> list[EnrichedApartment]:
    """Default runner: the bot (composition root) wires the Postgres
    checkpointer into the persistence-agnostic search graph."""
    return await run_search_graph(
        criteria, checkpointer_factory=get_async_postgres_checkpointer, **kwargs
    )


logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class SearchExecution:
    """Structured result returned by bot search service."""

    criteria: SearchCriteria
    apartments: list[EnrichedApartment]
    notices: tuple[str, ...] = ()


class SearchBotService:
    """Coordinates persistence and graph execution for Telegram flows."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        intent_node: IntentNode | None = None,
        search_runner: SearchRunner = run_search_graph_with_postgres,
        notion_sync: NotionApartmentSync | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._intent_node = intent_node or IntentNode()
        self._search_runner = search_runner
        self._monitor = MonitorService(session_factory=session_factory)
        self._feedback = FeedbackService(
            session_factory=session_factory, notion_sync=notion_sync
        )
        self._recommendation = RecommendationService(
            session_factory=session_factory,
            get_active_criteria=self.get_active_criteria,
            run_search=self._run_search_graph,
        )

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
        parsed = await self._intent_node.parse_with_metadata(
            user_id=telegram_user_id,
            message=query,
        )
        notices = (
            (
                "Город не удалось распознать, поэтому использую Алматы. "
                "Уточнить город можно через /refine."
            ),
        ) if parsed.defaulted_city else ()
        return await self._persist_and_run_search(
            telegram_user_id=telegram_user_id,
            username=username,
            criteria=parsed.criteria,
            notices=notices,
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

        criteria = await self._intent_node.refine(
            criteria=active_criteria,
            message=message,
        )
        if criteria == active_criteria and not await self._message_has_search_signal(
            telegram_user_id=telegram_user_id,
            message=message,
        ):
            # Nothing changed AND the message carried no recognizable criteria
            # (e.g. "привет"): keep the user in refine mode with a hint. A full
            # restatement that happens to match the active criteria (e.g. the same
            # "2-комнатная в Алматы до 45 млн") is treated as "search again" below.
            msg = "refinement did not change supported criteria"
            raise CriteriaUnchangedError(msg)
        return await self._persist_and_run_search(
            telegram_user_id=telegram_user_id,
            username=username,
            criteria=criteria,
        )

    async def _save_active_criteria(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        criteria: SearchCriteria,
    ) -> SearchCriteria:
        """Persist updated active criteria without running a search."""
        async with self._session_factory() as session:
            user = await upsert_telegram_user(
                session, telegram_user_id=telegram_user_id, username=username
            )
            await replace_active_search_criteria(
                session,
                user_id=user.id,
                criteria_payload=criteria.model_dump(mode="json"),
            )
            await session.commit()
        return criteria

    async def _require_active_criteria(self, *, telegram_user_id: int) -> SearchCriteria:
        active = await self.get_active_criteria(telegram_user_id=telegram_user_id)
        if active is None:
            msg = "active criteria not found"
            raise ActiveCriteriaNotFoundError(msg)
        return active

    async def set_active_city(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        city_text: str,
    ) -> tuple[SearchCriteria, bool]:
        """Set the active city from a canonical name or free text (typo-tolerant).

        Changing the city clears districts (they are city-specific). Returns the
        (possibly unchanged) criteria and whether the city resolved.
        """
        active = await self._require_active_criteria(telegram_user_id=telegram_user_id)
        canonical = LOCATIONS.canonical_city(city_text) or LOCATIONS.fuzzy_city(city_text)
        if canonical is None:
            return active, False
        updated = active.model_copy(update={"city": canonical, "districts": None})
        saved = await self._save_active_criteria(
            telegram_user_id=telegram_user_id, username=username, criteria=updated
        )
        return saved, True

    async def set_active_deal_type(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        deal_type: str,
        rent_period: str | None = None,
    ) -> tuple[SearchCriteria, bool]:
        """Set the active deal type (sale/rent) and, for rent, the term.

        Switching sale<->rent — or the rent term (300K/мес vs 15K/сутки) —
        invalidates the old budget, so it is cleared for the user to enter a
        fitting one. Returns the saved criteria and whether the budget was reset.
        """
        active = await self._require_active_criteria(telegram_user_id=telegram_user_id)
        new_period = rent_period if deal_type == "rent" else None
        changed = deal_type != active.deal_type or new_period != active.rent_period
        update: dict[str, object] = {"deal_type": deal_type, "rent_period": new_period}
        budget_reset = changed and (
            active.min_price_kzt is not None or active.max_price_kzt is not None
        )
        if changed:
            update["min_price_kzt"] = None
            update["max_price_kzt"] = None
        updated = active.model_copy(update=update)
        saved = await self._save_active_criteria(
            telegram_user_id=telegram_user_id, username=username, criteria=updated
        )
        return saved, budget_reset

    async def set_active_district(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        district: str | None,
    ) -> SearchCriteria:
        """Set (or clear, when district is None) the active district."""
        active = await self._require_active_criteria(telegram_user_id=telegram_user_id)
        districts = [district] if district is not None else None
        updated = active.model_copy(update={"districts": districts})
        return await self._save_active_criteria(
            telegram_user_id=telegram_user_id, username=username, criteria=updated
        )

    async def toggle_active_owner_only(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
    ) -> SearchCriteria:
        """Flip the owner-only filter (krisha's "от хозяев") on active criteria."""
        active = await self._require_active_criteria(telegram_user_id=telegram_user_id)
        updated = active.model_copy(update={"owner_only": not active.owner_only})
        return await self._save_active_criteria(
            telegram_user_id=telegram_user_id, username=username, criteria=updated
        )

    async def apply_refinement_value(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        message: str,
    ) -> SearchCriteria:
        """Merge a typed value (rooms/budget/area/city) into active criteria.

        Reuses the intent refiner so "до 45 млн", "2-3 комнаты", "от 50 м²" etc.
        parse the same as a free-form refinement. Persists but does not search.
        """
        active = await self._require_active_criteria(telegram_user_id=telegram_user_id)
        refined = await self._intent_node.refine(criteria=active, message=message)
        return await self._save_active_criteria(
            telegram_user_id=telegram_user_id, username=username, criteria=refined
        )

    async def _message_has_search_signal(
        self,
        *,
        telegram_user_id: int,
        message: str,
    ) -> bool:
        """True if the message names any concrete criterion (rooms/price/area/
        district) or an explicit city — used to tell a valid restatement apart
        from unrecognizable refinement text."""
        parsed = await self._intent_node.parse_with_metadata(
            user_id=telegram_user_id,
            message=message,
        )
        criteria = parsed.criteria
        return bool(
            criteria.rooms
            or criteria.min_price_kzt is not None
            or criteria.max_price_kzt is not None
            or criteria.min_area_m2 is not None
            or criteria.max_area_m2 is not None
            or criteria.districts
            or not parsed.defaulted_city
        )

    async def rerun_active_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
    ) -> SearchExecution:
        """Re-run the current active criteria to fetch the next batch.

        Same criteria as the last search; per-user dedup hides listings the user
        has already been shown, so this surfaces new results (the "show more"
        action). Raises ActiveCriteriaNotFoundError if the user has not searched yet.
        """
        active_criteria = await self.get_active_criteria(telegram_user_id=telegram_user_id)
        if active_criteria is None:
            msg = "active criteria not found"
            raise ActiveCriteriaNotFoundError(msg)
        return await self._persist_and_run_search(
            telegram_user_id=telegram_user_id,
            username=username,
            criteria=active_criteria,
        )

    async def _persist_and_run_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        criteria: SearchCriteria,
        notices: tuple[str, ...] = (),
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

        apartments = await self._run_search_graph(
            telegram_user_id=telegram_user_id,
            user_id=user_id,
            criteria=criteria,
        )
        return SearchExecution(
            criteria=criteria,
            apartments=apartments,
            notices=notices,
        )

    async def _run_search_graph(
        self,
        *,
        telegram_user_id: int,
        user_id: int,
        criteria: SearchCriteria,
        dedup_namespace: str = "search",
    ) -> list[EnrichedApartment]:
        """Run the search graph and drop listings the user already decided on."""
        try:
            runner_kwargs = {
                "thread_id": f"telegram-user:{telegram_user_id}",
                "checkpoint_ns": "telegram-search",
            }
            if dedup_namespace != "search":
                runner_kwargs["dedup_namespace"] = dedup_namespace
            apartments = await self._search_runner(criteria, **runner_kwargs)
        except SearchExecutionError:
            raise
        except AntiBotBlockedError as exc:
            logger.warning(
                "Krisha anti-bot block for telegram user %s",
                telegram_user_id,
            )
            raise SearchExecutionError(SEARCH_BLOCKED_MESSAGE) from exc
        except Exception as exc:
            logger.exception(
                "Search runner failed for telegram user %s",
                telegram_user_id,
            )
            raise SearchExecutionError() from exc
        if not apartments:
            return apartments
        async with self._session_factory() as session:
            records = await upsert_apartment_records(session, apartments=apartments)
            feedback_map = await get_apartment_feedback_map(
                session,
                user_id=user_id,
                apartments=records,
            )
            await mark_apartments_seen(session, user_id=user_id, apartments=records)
            await session.commit()

        # Hide anything the user already decided on (saved or rejected) so the
        # same listings don't resurface in later manual searches.
        return [
            apartment
            for apartment, record in zip(apartments, records, strict=True)
            if feedback_map.get(record.id) is None
        ]

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

    # --- feedback (delegates to FeedbackService) -------------------------------

    async def get_saved_apartments(
        self, *, telegram_user_id: int, limit: int = 10
    ) -> list[EnrichedApartment]:
        """Return recently saved apartments for one Telegram user."""
        return await self._feedback.get_saved_apartments(
            telegram_user_id=telegram_user_id, limit=limit
        )

    async def count_saved_apartments(self, *, telegram_user_id: int) -> int:
        """Total number of apartments the user has saved."""
        return await self._feedback.count_saved_apartments(telegram_user_id=telegram_user_id)

    async def delete_saved_apartment(self, *, telegram_user_id: int, external_id: str) -> bool:
        """Remove one apartment from the user's saved list (soft delete; recoverable)."""
        return await self._feedback.delete_saved_apartment(
            telegram_user_id=telegram_user_id, external_id=external_id
        )

    async def get_trashed_apartments(
        self, *, telegram_user_id: int, limit: int = 10
    ) -> list[EnrichedApartment]:
        """Return recoverable apartments for the /trash list."""
        return await self._feedback.get_trashed_apartments(
            telegram_user_id=telegram_user_id, limit=limit
        )

    async def restore_apartment(
        self, *, telegram_user_id: int, external_id: str
    ) -> RestoreOutcome | None:
        """Bring one apartment back from the trash."""
        return await self._feedback.restore_apartment(
            telegram_user_id=telegram_user_id, external_id=external_id
        )

    async def purge_trashed_apartment(self, *, telegram_user_id: int, external_id: str) -> bool:
        """Permanently dismiss a trashed apartment ("delete forever")."""
        return await self._feedback.purge_trashed_apartment(
            telegram_user_id=telegram_user_id, external_id=external_id
        )

    async def save_apartment(
        self, *, telegram_user_id: int, username: str | None, external_id: str
    ) -> bool:
        """Save one apartment (by krisha external id) to the user's list."""
        return await self._feedback.save_apartment(
            telegram_user_id=telegram_user_id, username=username, external_id=external_id
        )

    async def reject_apartment(
        self, *, telegram_user_id: int, username: str | None, external_id: str
    ) -> bool:
        """Reject one apartment so it is hidden from future manual searches."""
        return await self._feedback.reject_apartment(
            telegram_user_id=telegram_user_id, username=username, external_id=external_id
        )

    async def save_apartments(
        self, *, telegram_user_id: int, username: str | None, apartment_urls: list[str]
    ) -> int:
        """Persist a positive decision for the current apartment selection."""
        return await self._feedback.save_apartments(
            telegram_user_id=telegram_user_id,
            username=username,
            apartment_urls=apartment_urls,
        )

    async def reject_apartments(
        self, *, telegram_user_id: int, username: str | None, apartment_urls: list[str]
    ) -> int:
        """Persist a negative decision for the current apartment selection."""
        return await self._feedback.reject_apartments(
            telegram_user_id=telegram_user_id,
            username=username,
            apartment_urls=apartment_urls,
        )

    # --- recommendations (delegates to RecommendationService) ------------------

    async def recommend(
        self, *, telegram_user_id: int, username: str | None, limit: int = 6
    ) -> RecommendationResult:
        """Recommend fresh listings ranked by the user's saved/rejected taste."""
        return await self._recommendation.recommend(
            telegram_user_id=telegram_user_id, username=username, limit=limit
        )

    # --- monitoring (delegates to MonitorService) ------------------------------

    async def get_monitor_status(self, *, telegram_user_id: int) -> MonitorStatus | None:
        """Return current monitoring settings for a Telegram user."""
        return await self._monitor.get_monitor_status(telegram_user_id=telegram_user_id)

    async def set_monitor_enabled(
        self, *, telegram_user_id: int, username: str | None, enabled: bool
    ) -> MonitorStatus:
        """Create or update monitor flag for a Telegram user."""
        return await self._monitor.set_monitor_enabled(
            telegram_user_id=telegram_user_id, username=username, enabled=enabled
        )

    async def set_monitor_interval(
        self, *, telegram_user_id: int, username: str | None, interval_minutes: int
    ) -> MonitorStatus:
        """Create or update monitor interval for a Telegram user."""
        return await self._monitor.set_monitor_interval(
            telegram_user_id=telegram_user_id,
            username=username,
            interval_minutes=interval_minutes,
        )

    def get_default_monitor_status(self) -> MonitorStatus:
        """Return default monitor settings when nothing is stored yet."""
        return self._monitor.get_default_monitor_status()
