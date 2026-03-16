"""Tests for search graph checkpointing helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent.graph import get_search_graph_state_history, run_search_graph
from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.score import ApartmentScore
from agent.nodes.scoring_node import ScoringNode
from agent.nodes.search_node import SearchNode
from db import build_checkpoint_config


class FakeParser:
    """Fake parser used to create deterministic graph state."""

    def __init__(self, apartments: list[Apartment]) -> None:
        self._apartments = apartments

    async def search(self, context: object, criteria: SearchCriteria) -> list[Apartment]:
        del context, criteria
        return self._apartments


class FakeApartmentScorer:
    """Deterministic scorer for checkpoint tests."""

    def __init__(self, score: ApartmentScore) -> None:
        self._score = score

    async def score_apartment(self, apartment) -> ApartmentScore:
        del apartment
        return self._score


def make_context_factory(context: object):
    @asynccontextmanager
    async def factory():
        yield context

    return factory


def build_criteria() -> SearchCriteria:
    return SearchCriteria(
        user_id=17,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        page_limit=1,
    )


def build_apartment() -> Apartment:
    return Apartment(
        external_id="700800",
        source="krisha",
        url="https://krisha.kz/a/show/700800",
        title="Checkpoint test apartment",
        price_kzt=29_000_000,
        city="Almaty",
        address="Abay 7",
        rooms=2,
        area_m2=54.0,
        floor="6/9",
        photos=["https://photos.krisha.kz/700800/1.jpg"],
        published_at=datetime(2025, 3, 1, tzinfo=UTC),
    )


def build_score() -> ApartmentScore:
    return ApartmentScore(
        score=72.0,
        reasons=["reasonable price", "solid district"],
        recommendation="consider",
    )


@pytest.mark.asyncio
async def test_run_search_graph_persists_checkpoints_with_thread_id() -> None:
    checkpointer = InMemorySaver()
    search_node = SearchNode(
        parser=FakeParser([build_apartment()]),
        context_factory=make_context_factory(object()),
    )
    scoring_node = ScoringNode(scorer=FakeApartmentScorer(build_score()))

    result = await run_search_graph(
        build_criteria(),
        search_node=search_node,
        scoring_node=scoring_node,
        checkpointer=checkpointer,
        thread_id="thread-1",
    )

    assert len(result) == 1
    assert result[0].score is not None

    history = await get_search_graph_state_history(
        thread_id="thread-1",
        search_node=search_node,
        scoring_node=scoring_node,
        checkpointer=checkpointer,
    )

    assert len(history) >= 1
    latest_values = history[0].values
    assert "enriched_apartments" in latest_values


def test_build_checkpoint_config_sets_expected_keys() -> None:
    config = build_checkpoint_config(
        thread_id="thread-42",
        checkpoint_ns="search",
        checkpoint_id="checkpoint-7",
    )

    assert config["configurable"]["thread_id"] == "thread-42"
    assert config["configurable"]["checkpoint_ns"] == "search"
    assert config["configurable"]["checkpoint_id"] == "checkpoint-7"
