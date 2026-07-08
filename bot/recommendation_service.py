"""Recommendation service: /foryou picks ranked by the user's saved/rejected taste."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from bot.errors import ActiveCriteriaNotFoundError, NoPreferencesError
from bot.preferences import build_preference_profile, build_taste_criteria, rank_by_preference
from db import list_feedback_apartments, upsert_telegram_user

# Search execution is the search service's concern; recommendation composes it.
# The runner receives (telegram_user_id, user_id, criteria, dedup_namespace).
TasteSearchRunner = Callable[..., Awaitable[list[EnrichedApartment]]]
ActiveCriteriaProvider = Callable[..., Awaitable[SearchCriteria | None]]


@dataclass(slots=True, frozen=True)
class Recommendation:
    """One /foryou pick with the reasons it matched the user's taste."""

    apartment: EnrichedApartment
    reasons: list[str]


@dataclass(slots=True, frozen=True)
class RecommendationResult:
    """Result of /foryou: candidates ordered by the user's saved/rejected taste."""

    criteria: SearchCriteria
    recommendations: list[Recommendation]


class RecommendationService:
    """Owns /foryou: learns taste from feedback, searches, and ranks candidates."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        get_active_criteria: ActiveCriteriaProvider,
        run_search: TasteSearchRunner,
    ) -> None:
        self._session_factory = session_factory
        self._get_active_criteria = get_active_criteria
        self._run_search = run_search

    async def recommend(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        # Match the /search presentation: everything the taste search fetches
        # (PARSER__MAX_RESULTS caps the pipeline at 6).
        limit: int = 6,
    ) -> RecommendationResult:
        """Recommend fresh listings ranked by the user's saved/rejected taste.

        Runs the user's active-criteria search (without touching active criteria),
        then orders candidates by how well they match what the user saved.
        """
        criteria = await self._get_active_criteria(telegram_user_id=telegram_user_id)
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
        candidates = await self._run_search(
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
