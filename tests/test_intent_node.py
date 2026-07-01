"""Tests for IntentNode and text-based search entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from agent.graph import run_search_graph_from_text
from agent.locations import LocationInputError
from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
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


class StubLLMIntentParser:
    """Minimal fake LLM parser that returns canned JSON-like payloads."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.calls: list[tuple[str, SearchCriteria | None]] = []

    async def parse_patch(
        self,
        *,
        message: str,
        existing_criteria: SearchCriteria | None = None,
    ) -> dict[str, object]:
        self.calls.append((message, existing_criteria))
        return dict(self._payload)


class BrokenLLMIntentParser:
    """Fake LLM parser that fails so regex fallback can be exercised."""

    async def parse_patch(
        self,
        *,
        message: str,
        existing_criteria: SearchCriteria | None = None,
    ) -> dict[str, object]:
        del message, existing_criteria
        msg = "llm unavailable"
        raise RuntimeError(msg)


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


@pytest.mark.asyncio
async def test_intent_node_parses_sale_message() -> None:
    node = IntentNode(llm_parser_factory=lambda: None)
    criteria = await node.parse(
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


@pytest.mark.asyncio
async def test_intent_node_parses_rent_message() -> None:
    node = IntentNode(llm_parser_factory=lambda: None)
    criteria = await node.parse(
        user_id=2,
        message="Нужна аренда 1 ком в Астане до 300 тыс тг, pages 2",
    )

    assert criteria.city == "Astana"
    assert criteria.deal_type == "rent"
    assert criteria.max_price_kzt == 300_000
    assert criteria.rooms == [1]
    assert criteria.page_limit == 2


@pytest.mark.asyncio
async def test_intent_node_reports_when_default_city_is_used() -> None:
    node = IntentNode(llm_parser_factory=lambda: None)

    parsed = await node.parse_with_metadata(
        user_id=2,
        message="Нужна 2-комнатная до 30 млн",
    )

    assert parsed.criteria.city == "Almaty"
    assert parsed.defaulted_city is True


@pytest.mark.parametrize(
    ("message", "expected_city"),
    [
        ("куплю 2 ком в Павлодаре до 20 млн", "Pavlodar"),
        ("квартира в Уральске", "Uralsk"),
        ("аренда в Усть-Каменогорске", "Ust-Kamenogorsk"),
        ("ищу 3 ком в Костанае", "Kostanay"),
        ("квартира в Кызылорде", "Kyzylorda"),
        ("Актобе 1 комнатную", "Aktobe"),
        ("куплю в Таразе", "Taraz"),
    ],
)
@pytest.mark.asyncio
async def test_intent_node_recognizes_kazakhstan_cities(
    message: str, expected_city: str
) -> None:
    node = IntentNode(llm_parser_factory=lambda: None)
    criteria = await node.parse(user_id=1, message=message)
    assert criteria.city == expected_city


@pytest.mark.asyncio
async def test_regex_fallback_recognizes_new_catalog_city_and_district() -> None:
    node = IntentNode(llm_parser_factory=lambda: None)

    city_only = await node.parse(
        user_id=1,
        message="двухкомнатная в Қонаеве до 30 млн",
    )
    district = await node.parse(
        user_id=1,
        message="квартира в Актобе, Алматинский район",
    )

    assert city_only.city == "Konaev"
    assert city_only.districts is None
    assert district.city == "Aktobe"
    assert district.districts == ["Almaty"]


@pytest.mark.asyncio
async def test_llm_location_text_is_validated_against_catalog() -> None:
    node = IntentNode(
        llm_parser=StubLLMIntentParser(
            {
                "city": "Астана",
                "districts": ["Есильский район"],
                "rooms": [2],
            }
        )
    )

    criteria = await node.parse(user_id=1, message="двухкомнатная в Астане")

    assert criteria.city == "Astana"
    assert criteria.districts == ["Yesil"]


@pytest.mark.asyncio
async def test_intent_rejects_city_district_mismatch_from_llm() -> None:
    node = IntentNode(
        llm_parser=StubLLMIntentParser(
            {"city": "Астана", "districts": ["Бостандыкский район"]}
        )
    )

    with pytest.raises(LocationInputError, match="не относится"):
        await node.parse(user_id=1, message="Астана, Бостандыкский район")


@pytest.mark.asyncio
async def test_intent_reports_city_missing_from_krisha() -> None:
    node = IntentNode(llm_parser_factory=lambda: None)

    with pytest.raises(LocationInputError, match="Krisha"):
        await node.parse(user_id=1, message="квартира в Жеме")


@pytest.mark.asyncio
async def test_intent_node_parses_hyphenated_room_count() -> None:
    node = IntentNode(llm_parser_factory=lambda: None)
    criteria = await node.parse(
        user_id=3,
        message="2-комнатная квартира в Алматы до 45 млн",
    )

    assert criteria.rooms == [2]
    assert criteria.max_price_kzt == 45_000_000


@pytest.mark.asyncio
async def test_intent_node_refines_existing_criteria() -> None:
    node = IntentNode(llm_parser_factory=lambda: None)
    base = SearchCriteria(
        user_id=10,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        min_price_kzt=25_000_000,
        max_price_kzt=45_000_000,
        rooms=[2, 3],
        districts=["Bostandyk"],
        min_area_m2=50.0,
        max_area_m2=80.0,
        page_limit=3,
    )

    refined = await node.refine(
        criteria=base,
        message="только 3 комнаты, район Медеу и до 35 млн, pages 5",
    )

    assert refined.city == "Almaty"
    assert refined.deal_type == "sale"
    assert refined.min_price_kzt == 25_000_000
    assert refined.max_price_kzt == 35_000_000
    assert refined.rooms == [3]
    assert refined.districts == ["Medeu"]
    assert refined.page_limit == 5


@pytest.mark.asyncio
async def test_intent_node_call_updates_state_with_criteria() -> None:
    node = IntentNode(llm_parser_factory=lambda: None)
    result = await node({"user_id": 7, "message": "Куплю квартиру в Алматы"})

    assert result["user_id"] == 7
    assert result["criteria"].city == "Almaty"
    assert result["criteria"].deal_type == "sale"


@pytest.mark.asyncio
async def test_intent_node_uses_llm_parser_for_word_forms_and_unknown_city() -> None:
    llm_parser = StubLLMIntentParser(
        {
            "city": "Karaganda",
            "deal_type": "sale",
            "max_price_kzt": 30_000_000,
            "rooms": [2],
        }
    )
    node = IntentNode(llm_parser=llm_parser)

    criteria = await node.parse(
        user_id=11,
        message="двухкомнатная в Караганде до 30 млн",
    )

    assert criteria.city == "Karaganda"
    assert criteria.deal_type == "sale"
    assert criteria.max_price_kzt == 30_000_000
    assert criteria.rooms == [2]
    assert llm_parser.calls == [("двухкомнатная в Караганде до 30 млн", None)]


@pytest.mark.asyncio
async def test_intent_node_uses_llm_parser_for_complex_room_query() -> None:
    llm_parser = StubLLMIntentParser(
        {
            "city": "Almaty",
            "deal_type": "sale",
            "rooms": [2, 3],
        }
    )
    node = IntentNode(llm_parser=llm_parser)

    criteria = await node.parse(
        user_id=12,
        message="квартира на Розыбакиева 2-3 комнаты",
    )

    assert criteria.city == "Almaty"
    assert criteria.rooms == [2, 3]


@pytest.mark.asyncio
async def test_intent_node_falls_back_to_regex_when_llm_parser_errors() -> None:
    node = IntentNode(llm_parser=BrokenLLMIntentParser())

    criteria = await node.parse(
        user_id=13,
        message="2-комнатная квартира в Алматы до 45 млн",
    )

    assert criteria.city == "Almaty"
    assert criteria.max_price_kzt == 45_000_000
    assert criteria.rooms == [2]


@pytest.mark.asyncio
async def test_intent_node_falls_back_to_regex_when_llm_factory_raises() -> None:
    def failing_factory() -> Any:
        msg = "missing key"
        raise RuntimeError(msg)

    node = IntentNode(llm_parser_factory=failing_factory)
    criteria = await node.parse(
        user_id=14,
        message="2-комнатная квартира в Алматы до 45 млн",
    )

    assert criteria.city == "Almaty"
    assert criteria.max_price_kzt == 45_000_000
    assert criteria.rooms == [2]


@pytest.mark.asyncio
async def test_run_search_graph_from_text_uses_intent_output() -> None:
    parser = FakeSearchParser([build_apartment()])
    search_node = SearchNode(parser=parser, context_factory=make_context_factory(object()))

    result = await run_search_graph_from_text(
        user_id=77,
        message="Нужна аренда 2 комнаты в Астане до 400 тыс тг",
        intent_node=IntentNode(llm_parser_factory=lambda: None),
        search_node=search_node,
    )

    assert len(result) == 1
    assert result[0].apartment.external_id == "200300"
    assert parser.last_city == "Astana"
    assert parser.last_deal_type == "rent"
    assert parser.last_max_price == 400_000
    assert parser.last_rooms == [2]
