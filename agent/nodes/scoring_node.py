"""Scoring node for enriched apartments."""

from __future__ import annotations

import asyncio
from typing import Protocol

from agent.models.enriched import EnrichedApartment
from agent.models.score import ApartmentScore
from agent.nodes.search_node import SearchGraphState
from agent.tools.gemini_scorer import GeminiApartmentScorer
from config.settings import get_settings


class ApartmentScorerProtocol(Protocol):
    """Contract required by scoring node."""

    async def score_apartment(self, apartment: EnrichedApartment) -> ApartmentScore: ...


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

        scored = await asyncio.gather(*(self._score_item(item) for item in source_items))
        return {
            "criteria": state["criteria"],
            "apartments": state.get("apartments", []),
            "enriched_apartments": scored,
        }

    async def _score_item(self, item: EnrichedApartment) -> EnrichedApartment:
        try:
            score = await self._scorer.score_apartment(item)
        except Exception:
            score = None
        return item.model_copy(update={"score": score})


def create_default_scoring_node() -> ScoringNode:
    """Create Gemini-backed scoring node from settings."""
    settings = get_settings()
    scorer = GeminiApartmentScorer(
        api_key=settings.api.gemini_api_key.get_secret_value(),
        model=settings.scoring.model,
        temperature=settings.scoring.temperature,
        timeout_seconds=settings.scoring.timeout_seconds,
    )
    return ScoringNode(scorer=scorer)
