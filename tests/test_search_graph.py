"""Tests for SearchNode and LangGraph search pipeline."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from agent.graph import run_search_graph
from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.nodes.search_node import SearchNode


class FakeParser:
    """Fake parser used for graph/node tests."""

    def __init__(self, apartments: list[Apartment]) -> None:
        self.apartments = apartments
        self.calls: list[tuple[object, SearchCriteria]] = []

    async def search(self, context: object, criteria: SearchCriteria) -> list[Apartment]:
        self.calls.append((context, criteria))
        return self.apartments


def make_context_factory(context: object):
    """Build context-manager factory compatible with SearchNode."""

    @asynccontextmanager
    async def factory():
        yield context

    return factory


def build_criteria() -> SearchCriteria:
    return SearchCriteria(
        user_id=7,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        page_limit=1,
    )


def build_apartment() -> Apartment:
    return Apartment(
        external_id="100500",
        source="krisha",
        url="https://krisha.kz/a/show/100500",
        title="2-room apartment",
        price_kzt=33_000_000,
        city="Almaty",
        district="Bostandyk",
        address="Navoi 99",
        area_m2=57.0,
        floor="4/9",
        rooms=2,
        photos=["https://photos.krisha.kz/100500/1.jpg"],
        published_at=datetime(2025, 1, 10, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_search_node_calls_parser_and_returns_state() -> None:
    criteria = build_criteria()
    apartment = build_apartment()
    parser = FakeParser([apartment])
    context = object()
    node = SearchNode(parser=parser, context_factory=make_context_factory(context))

    result = await node({"criteria": criteria, "apartments": []})

    assert result["criteria"] == criteria
    assert result["apartments"] == [apartment]
    assert len(parser.calls) == 1
    used_context, used_criteria = parser.calls[0]
    assert used_context is context
    assert used_criteria == criteria


@pytest.mark.asyncio
async def test_run_search_graph_wraps_apartments_to_enriched() -> None:
    criteria = build_criteria()
    apartment = build_apartment()
    parser = FakeParser([apartment])
    node = SearchNode(parser=parser, context_factory=make_context_factory(object()))

    enriched = await run_search_graph(criteria, search_node=node)

    assert len(enriched) == 1
    assert enriched[0].apartment.external_id == "100500"
    assert enriched[0].score is None


@pytest.mark.asyncio
async def test_run_search_graph_returns_empty_when_parser_empty() -> None:
    criteria = build_criteria()
    parser = FakeParser([])
    node = SearchNode(parser=parser, context_factory=make_context_factory(object()))

    enriched = await run_search_graph(criteria, search_node=node)

    assert enriched == []
    assert len(parser.calls) == 1
