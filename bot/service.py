"""Application service for Telegram bot workflows."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.graph import run_search_graph
from agent.locations import LOCATIONS
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.nodes.intent_node import IntentNode
from agent.tools.krisha_parser import AntiBotBlockedError
from bot.monitoring import DEFAULT_MONITOR_INTERVAL_MINUTES
from bot.preferences import build_preference_profile, build_taste_criteria, rank_by_preference
from db import (
    ApartmentDecision,
    clear_apartment_feedback,
    count_feedback_apartments,
    delete_apartment_feedback,
    get_active_search_criteria_record,
    get_apartment_feedback_map,
    get_monitor_settings_record,
    list_apartment_records_by_external_ids,
    list_apartment_records_by_urls,
    list_feedback_apartments,
    list_trashed_apartments,
    mark_apartments_seen,
    replace_active_search_criteria,
    restore_apartment_feedback,
    tombstone_apartment_feedback,
    update_apartment_feedback_notion_sync,
    upsert_apartment_feedback,
    upsert_apartment_records,
    upsert_monitor_settings,
    upsert_telegram_user,
)

SearchRunner = Callable[..., Awaitable[list[EnrichedApartment]]]
# What a /trash restore actually did: a deleted-from-saved item goes back to the
# saved list; a rejected item has its rejection lifted so it can reappear in search.
RestoreOutcome = Literal["restored_to_saved", "unrejected"]
SEARCH_EXECUTION_ERROR_MESSAGE = (
    "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c "
    "\u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c "
    "\u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u044f. "
    "\u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439 \u043f\u043e\u0437\u0436\u0435."
)
SEARCH_BLOCKED_MESSAGE = (
    "\u0421\u0430\u0439\u0442 \u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e "
    "\u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0438\u043b "
    "\u0434\u043e\u0441\u0442\u0443\u043f \u0438\u0437-\u0437\u0430 "
    "\u0437\u0430\u0449\u0438\u0442\u044b \u043e\u0442 \u0431\u043e\u0442\u043e\u0432. "
    "\u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439 "
    "\u043f\u043e\u0437\u0436\u0435."
)
logger = logging.getLogger(__name__)


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
    notices: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class MonitorStatus:
    """Persistent monitoring settings exposed to bot handlers."""

    enabled: bool
    interval_minutes: int


@dataclass(slots=True, frozen=True)
class Recommendation:
    """One preference-ranked apartment plus the reasons it fits the user."""

    apartment: EnrichedApartment
    reasons: list[str]


@dataclass(slots=True, frozen=True)
class RecommendationResult:
    """Result of /foryou: candidates ordered by the user's saved/rejected taste."""

    criteria: SearchCriteria
    recommendations: list[Recommendation]


class ActiveCriteriaNotFoundError(RuntimeError):
    """Raised when a refinement flow requires active criteria but none are stored."""


class CriteriaUnchangedError(ValueError):
    """Raised when refinement text did not change any supported criterion."""


class NoPreferencesError(RuntimeError):
    """Raised when /foryou has no saved apartments to learn the user's taste from."""


class SearchExecutionError(RuntimeError):
    """Raised when the upstream apartment search cannot complete."""

    def __init__(self, user_message: str = SEARCH_EXECUTION_ERROR_MESSAGE) -> None:
        super().__init__(user_message)
        self.user_message = user_message


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
    ) -> tuple[SearchCriteria, bool]:
        """Set the active deal type (sale/rent).

        Switching sale<->rent invalidates the old budget (45M to buy vs 300K/mo
        to rent), so it is cleared for the user to enter a fitting one. Returns
        the saved criteria and whether the budget was reset.
        """
        active = await self._require_active_criteria(telegram_user_id=telegram_user_id)
        changed = deal_type != active.deal_type
        update: dict[str, object] = {"deal_type": deal_type}
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

    async def count_saved_apartments(self, *, telegram_user_id: int) -> int:
        """Total number of apartments the user has saved."""
        async with self._session_factory() as session:
            return await count_feedback_apartments(
                session,
                telegram_user_id=telegram_user_id,
                decision="saved",
            )

    async def recommend(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        # Match the /search presentation: a short, high-confidence top-3 instead
        # of a long tail the user has to scroll through.
        limit: int = 3,
    ) -> RecommendationResult:
        """Recommend fresh listings ranked by the user's saved/rejected taste.

        Runs the user's active-criteria search (without touching active criteria),
        then orders candidates by how well they match what the user saved.
        """
        criteria = await self.get_active_criteria(telegram_user_id=telegram_user_id)
        if criteria is None:
            msg = "active criteria not found"
            raise ActiveCriteriaNotFoundError(msg)

        async with self._session_factory() as session:
            saved = await list_feedback_apartments(
                session, telegram_user_id=telegram_user_id, decision="saved", limit=100
            )
            rejected = await list_feedback_apartments(
                session, telegram_user_id=telegram_user_id, decision="rejected", limit=100
            )
            user = await upsert_telegram_user(
                session, telegram_user_id=telegram_user_id, username=username
            )
            user_id = user.id
            await session.commit()

        if not saved:
            msg = "no saved apartments to learn from"
            raise NoPreferencesError(msg)

        # Search by the learned taste, not by the volatile last-search criteria:
        # the user may have last searched a different city or rent vs purchase,
        # and reranking that result is not a recommendation. Candidates come from
        # the districts/rooms/price range of what the user actually saves.
        profile = build_preference_profile(saved, rejected)
        search_criteria = build_taste_criteria(profile, saved, base=criteria)
        candidates = await self._run_search_graph(
            telegram_user_id=telegram_user_id,
            user_id=user_id,
            criteria=search_criteria,
            dedup_namespace="foryou",
        )
        ranked = rank_by_preference(candidates, profile, criteria=search_criteria)[:limit]
        return RecommendationResult(
            criteria=search_criteria,
            recommendations=[
                Recommendation(apartment=item, reasons=reasons) for item, reasons in ranked
            ],
        )

    async def delete_saved_apartment(
        self,
        *,
        telegram_user_id: int,
        external_id: str,
    ) -> bool:
        """Remove one apartment from the user's saved list (soft delete; recoverable)."""
        async with self._session_factory() as session:
            removed = await delete_apartment_feedback(
                session,
                telegram_user_id=telegram_user_id,
                external_id=external_id,
            )
            await session.commit()
            return removed

    async def get_trashed_apartments(
        self,
        *,
        telegram_user_id: int,
        limit: int = 10,
    ) -> list[EnrichedApartment]:
        """Return recoverable apartments for the /trash list.

        Two kinds land here: items deleted from the saved list (soft-deleted
        "saved" feedback) and rejected items. Both can be brought back via
        :meth:`restore_apartment`. Rejected items come first (most likely the
        user's latest action), then deleted-from-saved, capped at ``limit``.
        """
        async with self._session_factory() as session:
            rejected = await list_feedback_apartments(
                session,
                telegram_user_id=telegram_user_id,
                decision="rejected",
                limit=limit,
            )
            deleted_saved = await list_trashed_apartments(
                session,
                telegram_user_id=telegram_user_id,
                limit=limit,
            )
        return (rejected + deleted_saved)[:limit]

    async def restore_apartment(
        self,
        *,
        telegram_user_id: int,
        external_id: str,
    ) -> RestoreOutcome | None:
        """Bring one apartment back from the trash.

        A deleted-from-saved item is un-deleted (back to the saved list); a
        rejected item has its rejection cleared so it can reappear in searches.
        Returns which happened, or None if nothing matched.
        """
        async with self._session_factory() as session:
            if await restore_apartment_feedback(
                session,
                telegram_user_id=telegram_user_id,
                external_id=external_id,
            ):
                await session.commit()
                return "restored_to_saved"
            if await clear_apartment_feedback(
                session,
                telegram_user_id=telegram_user_id,
                external_id=external_id,
                decision="rejected",
            ):
                await session.commit()
                return "unrejected"
            return None

    async def purge_trashed_apartment(
        self,
        *,
        telegram_user_id: int,
        external_id: str,
    ) -> bool:
        """Permanently dismiss a trashed apartment ("delete forever").

        Leaves /trash for good and stays hidden from search; not recoverable.
        Returns True if a trashed row was affected.
        """
        async with self._session_factory() as session:
            purged = await tombstone_apartment_feedback(
                session,
                telegram_user_id=telegram_user_id,
                external_id=external_id,
            )
            await session.commit()
            return purged

    async def save_apartment(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        external_id: str,
    ) -> bool:
        """Save one apartment (by krisha external id) to the user's list."""
        return await self._record_single_feedback(
            telegram_user_id=telegram_user_id,
            username=username,
            external_id=external_id,
            decision="saved",
        )

    async def reject_apartment(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        external_id: str,
    ) -> bool:
        """Reject one apartment so it is hidden from future manual searches."""
        return await self._record_single_feedback(
            telegram_user_id=telegram_user_id,
            username=username,
            external_id=external_id,
            decision="rejected",
        )

    async def _record_single_feedback(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        external_id: str,
        decision: ApartmentDecision,
    ) -> bool:
        async with self._session_factory() as session:
            records = await list_apartment_records_by_external_ids(
                session,
                external_ids=[external_id],
            )
        if not records:
            return False
        recorded = await self._record_apartment_feedback(
            telegram_user_id=telegram_user_id,
            username=username,
            apartment_urls=[records[0].url],
            decision=decision,
        )
        return recorded > 0

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
