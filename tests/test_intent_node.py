"""Tests for IntentNode and text-based search entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from agent.graph import run_search_graph_from_text
from agent.models.apartment import Apartment
from agent.nodes.intent_node import IntentNode
from agent.nodes.search_node import SearchNode


class FakeSearchParser:
    """Fake parser for SearchNode tests."""

    def __init__(self, apartments: list[Apartment]) -> None:
        self._apartments = apartments
        self.last_city: str | None = None
        self.last_deal_type: str | None = None
        self.last_max_price: int | None = None
        self.last_rooms: list[int] | None = None

    async def search(self, context: object, criteria) -> list[Apartment]:
        del context
        self.last_city = criteria.city
        self.last_deal_type = criteria.deal_type
        self.last_max_price = criteria.max_price_kzt
        self.last_rooms = criteria.rooms
        return self._apartments


def make_context_factory(context: object):
    @asynccontextmanager
    async def factory():
        yield context

    return factory


def build_apartment() -> Apartment:
    return Apartment(
        external_id="200300",
        source="krisha",
        url="https://krisha.kz/a/show/200300",
        title="Test apartment",
        price_kzt=25_000_000,
        city="Almaty",
        rooms=2,
        area_m2=55.0,
        floor="5/9",
        photos=["https://photos.krisha.kz/200300/1.jpg"],
        published_at=datetime(2025, 2, 5, tzinfo=UTC),
    )


def test_intent_node_parses_sale_message() -> None:
    node = IntentNode()
    criteria = node.parse(
        user_id=1,
        message=(
            "Ищу 2-3 комнатную квартиру в Алматы, Бостандыкский район, "
            "бюджет от 30 млн до 45 млн, площадь 50-80 м2"
        ),
    )

    assert criteria.user_id == 1
    assert criteria.city == "Almaty"
    assert criteria.deal_type == "sale"
    assert criteria.min_price_kzt == 30_000_000
    assert criteria.max_price_kzt == 45_000_000
    assert criteria.min_area_m2 == 50.0
    assert criteria.max_area_m2 == 80.0
    assert criteria.rooms == [2, 3]
    assert criteria.districts == ["Bostandyk"]


def test_intent_node_parses_rent_message() -> None:
    node = IntentNode()
    criteria = node.parse(
        user_id=2,
        message="Нужна аренда 1 ком в Астане до 300 тыс тг, pages 2",
    )

    assert criteria.city == "Astana"
    assert criteria.deal_type == "rent"
    assert criteria.max_price_kzt == 300_000
    assert criteria.rooms == [1]
    assert criteria.page_limit == 2


@pytest.mark.asyncio
async def test_intent_node_call_updates_state_with_criteria() -> None:
    node = IntentNode()
    result = await node({"user_id": 7, "message": "Куплю квартиру в Алматы"})

    assert result["user_id"] == 7
    assert result["criteria"].city == "Almaty"
    assert result["criteria"].deal_type == "sale"


@pytest.mark.asyncio
async def test_run_search_graph_from_text_uses_intent_output() -> None:
    parser = FakeSearchParser([build_apartment()])
    search_node = SearchNode(parser=parser, context_factory=make_context_factory(object()))

    result = await run_search_graph_from_text(
        user_id=77,
        message="Нужна аренда 2 комнаты в Астане до 400 тыс тг",
        intent_node=IntentNode(),
        search_node=search_node,
    )

    assert len(result) == 1
    assert result[0].apartment.external_id == "200300"
    assert parser.last_city == "Astana"
    assert parser.last_deal_type == "rent"
    assert parser.last_max_price == 400_000
    assert parser.last_rooms == [2]
