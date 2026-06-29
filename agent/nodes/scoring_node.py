"""Scoring node for enriched apartments."""

from __future__ import annotations

import asyncio
from typing import Protocol

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.models.score import ApartmentScore
from agent.nodes.search_node import SearchGraphState
from agent.tools.deepseek_scorer import DeepSeekApartmentScorer
from config.settings import get_settings


class ApartmentScorerProtocol(Protocol):
    """Contract required by scoring node."""

    async def score_apartment(
        self,
        apartment: EnrichedApartment,
        criteria: SearchCriteria | None = None,
    ) -> ApartmentScore: ...


class ScoringNode:
    """Assigns structured recommendation scores to enriched apartments."""

    def __init__(self, *, scorer: ApartmentScorerProtocol) -> None:
        self._scorer = scorer

    async def __call__(self, state: SearchGraphState) -> SearchGraphState:
        source_items = state.get("enriched_apartments")
        if source_items is None:
            source_items = [
                EnrichedApartment(apartment=apartment)
                for apartment in state["apartments"]
            ]

        if not source_items:
            return {
                "criteria": state["criteria"],
                "apartments": state.get("apartments", []),
                "enriched_apartments": [],
            }

        criteria = state["criteria"]
        scored = await asyncio.gather(
            *(self._score_item(item, criteria) for item in source_items)
        )
        ranked = sorted(scored, key=self._rank_key, reverse=True)
        return {
            "criteria": criteria,
            "apartments": state.get("apartments", []),
            "enriched_apartments": ranked,
        }

    async def _score_item(
        self,
        item: EnrichedApartment,
        criteria: SearchCriteria,
    ) -> EnrichedApartment:
        try:
            score = await self._scorer.score_apartment(item, criteria)
        except Exception:
            score = None
        return item.model_copy(update={"score": score})

    @staticmethod
    def _rank_key(item: EnrichedApartment) -> tuple[int, float]:
        """Sort scored apartments highest-first; unscored ones sink to the end."""
        if item.score is None:
            return (0, 0.0)
        return (1, item.score.score)


def create_default_scoring_node() -> ScoringNode:
    """Create DeepSeek-backed scoring node from settings."""
    settings = get_settings()
    scorer = DeepSeekApartmentScorer(
        api_key=settings.api.deepseek_api_key.get_secret_value(),
        model=settings.scoring.model,
        temperature=settings.scoring.temperature,
        timeout_seconds=settings.scoring.timeout_seconds,
    )
    return ScoringNode(scorer=scorer)
