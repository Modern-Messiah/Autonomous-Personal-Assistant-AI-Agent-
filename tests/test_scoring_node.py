"""Tests for DeepSeek scorer and scoring node."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest

from agent.graph import run_search_graph
from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.models.score import ApartmentScore
from agent.nodes.scoring_node import ScoringNode
from agent.nodes.search_node import SearchNode
from agent.tools.deepseek_scorer import DeepSeekApartmentScorer


class FakeParser:
    """Fake parser used to seed graph tests."""

    def __init__(self, apartments: list[Apartment]) -> None:
        self._apartments = apartments

    async def search(self, context: object, criteria: SearchCriteria) -> list[Apartment]:
        del context, criteria
        return self._apartments


class FakeApartmentScorer:
    """Deterministic scorer for node and graph tests."""

    def __init__(self, score: ApartmentScore) -> None:
        self._score = score

    async def score_apartments(
        self,
        apartments: list[EnrichedApartment],
        criteria: SearchCriteria | None = None,
    ) -> list[ApartmentScore | None]:
        del criteria
        return [self._score for _ in apartments]


class BrokenApartmentScorer:
    """Failing scorer to test fallback behavior."""

    async def score_apartments(
        self,
        apartments: list[EnrichedApartment],
        criteria: SearchCriteria | None = None,
    ) -> list[ApartmentScore | None]:
        del apartments, criteria
        raise RuntimeError("scoring failed")


def make_context_factory(context: object):
    @asynccontextmanager
    async def factory():
        yield context

    return factory


def build_criteria() -> SearchCriteria:
    return SearchCriteria(
        user_id=9,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        page_limit=1,
    )


def build_apartment() -> Apartment:
    return Apartment(
        external_id="500600",
        source="krisha",
        url="https://krisha.kz/a/show/500600",
        title="Scoring test apartment",
        price_kzt=37_000_000,
        city="Almaty",
        district="Bostandyk",
        address="Abylai Khan 2",
        rooms=2,
        area_m2=61.0,
        floor="8/12",
        photos=["https://photos.krisha.kz/500600/1.jpg"],
        published_at=datetime(2025, 2, 20, tzinfo=UTC),
    )


def build_enriched_apartment() -> EnrichedApartment:
    return EnrichedApartment(
        apartment=build_apartment(),
        nearby_schools=6,
        nearby_parks=4,
        nearby_metro=2,
        mortgage_monthly_payment_kzt=420_000,
        mortgage_total_overpayment_kzt=68_000_000,
    )


def build_score() -> ApartmentScore:
    return ApartmentScore(
        score=84.0,
        reasons=["good district", "strong nearby infrastructure"],
        recommendation="strong_buy",
    )


@pytest.mark.asyncio
async def test_deepseek_apartment_scorer_parses_structured_response() -> None:
    expected = build_score()
    batch = {
        "items": [
            {
                "index": 1,
                "score": expected.score,
                "recommendation": expected.recommendation,
                "reasons": expected.reasons,
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "deepseek.com" in str(request.url)
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": json.dumps(batch)}}]},
        )

    scorer = DeepSeekApartmentScorer(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )

    result = await scorer.score_apartments([build_enriched_apartment()])

    assert result == [expected]


def test_scorer_prompt_includes_batch_stats_and_comparative_reason_rules() -> None:
    scorer = DeepSeekApartmentScorer(api_key="test-key")
    one = build_enriched_apartment()
    two = build_enriched_apartment()
    two = two.model_copy(
        update={"apartment": two.apartment.model_copy(update={"price_kzt": 50_000_000})}
    )

    payload = scorer._build_payload([one, two], None)
    prompt = payload["messages"][1]["content"]

    # Aggregate stats ground the model's relative claims («на 18% ниже среднего»).
    assert "--- batch stats (this selection) ---" in prompt
    assert "price_per_m2 avg=" in prompt
    # Reason quality rules: comparative first reason, no vague filler, honest minus.
    assert "main differentiator" in prompt
    assert "Banned filler" in prompt
    assert "honest minus" in prompt

    # A single listing has nothing to compare against -> no stats block.
    solo_prompt = scorer._build_payload([one], None)["messages"][1]["content"]
    assert "--- batch stats" not in solo_prompt


def test_scorer_prompt_includes_description_and_market() -> None:
    scorer = DeepSeekApartmentScorer(api_key="test-key")
    item = build_enriched_apartment()
    item = item.model_copy(
        update={
            "apartment": item.apartment.model_copy(
                update={
                    "description": "Свежий ремонт, тёплая.\nТорг.",  # noqa: RUF001
                    "market_diff_percent": -9.2,
                    "build_year": 2019,
                    "building_type": "монолитный",
                    "furnished": "частично",
                }
            )
        }
    )

    prompt = scorer._build_payload([item], None)["messages"][1]["content"]

    # the full description reaches the model on its own line (newlines collapsed)
    assert "описание: Свежий ремонт, тёплая. Торг." in prompt  # noqa: RUF001
    assert "vs_city_market=9% cheaper than city" in prompt
    assert "build_year=2019" in prompt
    assert "furnished=частично" in prompt
    # days-on-market reaches the model as a fact and as scoring guidance
    assert "days_on_market=" in prompt
    assert "days_on_market is how many days the listing has been live" in prompt
    # guidance to weigh condition + krisha's benchmark
    assert "CONDITION" in prompt
    assert "vs_city_market is krisha's own verdict" in prompt


@pytest.mark.asyncio
async def test_scoring_node_attaches_scores_to_enriched_apartments() -> None:
    node = ScoringNode(scorer=FakeApartmentScorer(build_score()))
    apartment = build_enriched_apartment()

    result = await node(
        {
            "criteria": build_criteria(),
            "apartments": [apartment.apartment],
            "enriched_apartments": [apartment],
        }
    )

    assert len(result["enriched_apartments"]) == 1
    assert result["enriched_apartments"][0].score is not None
    assert result["enriched_apartments"][0].score.recommendation == "strong_buy"


@pytest.mark.asyncio
async def test_scoring_node_falls_back_to_none_on_errors() -> None:
    node = ScoringNode(scorer=BrokenApartmentScorer())
    apartment = build_enriched_apartment()

    result = await node(
        {
            "criteria": build_criteria(),
            "apartments": [apartment.apartment],
            "enriched_apartments": [apartment],
        }
    )

    assert result["enriched_apartments"][0].score is None


class AreaScorer:
    """Scores by area so ranking order is deterministic; area 0 -> None."""

    async def score_apartments(
        self,
        apartments: list[EnrichedApartment],
        criteria: SearchCriteria | None = None,
    ) -> list[ApartmentScore | None]:
        del criteria
        scores: list[ApartmentScore | None] = []
        for item in apartments:
            area = item.apartment.area_m2 or 0.0
            if area == 0.0:
                scores.append(None)
            else:
                scores.append(
                    ApartmentScore(score=area, reasons=["a", "b"], recommendation="consider")
                )
        return scores


@pytest.mark.asyncio
async def test_scoring_node_ranks_results_by_score_desc() -> None:
    def enriched(external_id: str, area: float) -> EnrichedApartment:
        apartment = build_apartment().model_copy(
            update={
                "external_id": external_id,
                "url": f"https://krisha.kz/a/show/{external_id}",
                "area_m2": area,
            }
        )
        return EnrichedApartment(apartment=apartment)

    items = [
        enriched("low", 30.0),
        enriched("none", 0.0),
        enriched("high", 90.0),
        enriched("mid", 60.0),
    ]
    node = ScoringNode(scorer=AreaScorer())

    result = await node(
        {
            "criteria": build_criteria(),
            "apartments": [item.apartment for item in items],
            "enriched_apartments": items,
        }
    )

    ranked = result["enriched_apartments"]
    assert [item.apartment.external_id for item in ranked] == ["high", "mid", "low", "none"]
    assert ranked[-1].score is None


@pytest.mark.asyncio
async def test_run_search_graph_uses_scoring_node_when_provided() -> None:
    apartment = build_apartment()
    search_node = SearchNode(
        parser=FakeParser([apartment]),
        context_factory=make_context_factory(object()),
    )
    scoring_node = ScoringNode(scorer=FakeApartmentScorer(build_score()))

    result = await run_search_graph(
        build_criteria(),
        search_node=search_node,
        scoring_node=scoring_node,
    )

    assert len(result) == 1
    assert result[0].apartment.external_id == "500600"
    assert result[0].score is not None
    assert result[0].score.score == 84.0
