"""Tests for bot service and formatting helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from bot.formatters import format_criteria, format_search_results, format_start_message
from bot.service import SearchBotService


class FakeSessionFactory:
    """Minimal async session factory for service tests."""

    def __init__(self) -> None:
        self.session = FakeSession()

    def __call__(self) -> FakeSession:
        return self.session


class FakeSession:
    """Async context manager with commit tracking."""

    def __init__(self) -> None:
        self.commit_calls = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb

    async def commit(self) -> None:
        self.commit_calls += 1


def build_apartment() -> EnrichedApartment:
    return EnrichedApartment(
        apartment=Apartment(
            external_id="900100",
            source="krisha",
            url="https://krisha.kz/a/show/900100",
            title="Bot test apartment",
            price_kzt=31_000_000,
            city="Almaty",
            rooms=2,
            area_m2=53.0,
            floor="5/9",
            photos=["https://photos.krisha.kz/900100/1.jpg"],
        ),
        nearby_schools=5,
        nearby_parks=3,
        nearby_metro=1,
    )


@pytest.mark.asyncio
async def test_search_bot_service_registers_and_runs_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(
        session_factory=session_factory,
        search_runner=fake_search_runner,
    )

    async def fake_upsert(session, *, telegram_user_id: int, username: str | None):
        del session, telegram_user_id, username
        return SimpleNamespace(id=123)

    stored_payloads: list[dict[str, object]] = []

    async def fake_replace(session, *, user_id: int, criteria_payload):
        del session
        assert user_id == 123
        stored_payloads.append(dict(criteria_payload))
        return SimpleNamespace()

    monkeypatch.setattr("bot.service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.service.replace_active_search_criteria", fake_replace)

    result = await service.run_search(
        telegram_user_id=77,
        username="tester",
        query="2-комнатная квартира в Алматы до 40 млн",
    )

    assert result.criteria.city == "Almaty"
    assert result.criteria.max_price_kzt == 40_000_000
    assert len(result.apartments) == 1
    assert stored_payloads[0]["city"] == "Almaty"
    assert session_factory.session.commit_calls == 1


@pytest.mark.asyncio
async def test_search_bot_service_loads_active_criteria(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)

    async def fake_get_record(session, *, telegram_user_id: int):
        del session, telegram_user_id
        return SimpleNamespace(
            criteria={
                "user_id": 77,
                "city": "Astana",
                "deal_type": "rent",
                "property_type": "apartment",
                "min_price_kzt": None,
                "max_price_kzt": 300_000,
                "rooms": [1],
                "districts": None,
                "min_area_m2": None,
                "max_area_m2": None,
                "page_limit": 2,
            }
        )

    monkeypatch.setattr("bot.service.get_active_search_criteria_record", fake_get_record)

    criteria = await service.get_active_criteria(telegram_user_id=77)

    assert criteria is not None
    assert criteria.city == "Astana"
    assert criteria.deal_type == "rent"


def test_formatters_render_expected_content() -> None:
    criteria = SearchCriteria(
        user_id=77,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        max_price_kzt=40_000_000,
        rooms=[2, 3],
        page_limit=3,
    )
    text = format_criteria(criteria)
    results_text = format_search_results([build_apartment()])

    assert "/search" in format_start_message()
    assert "Алматы" not in text
    assert "Город: Almaty" in text
    assert "40 000 000 KZT" in text
    assert "Bot test apartment" in results_text


async def fake_search_runner(
    criteria: SearchCriteria,
    *,
    thread_id: str,
    checkpoint_ns: str,
) -> list[EnrichedApartment]:
    assert criteria.city == "Almaty"
    assert thread_id == "telegram-user:77"
    assert checkpoint_ns == "telegram-search"
    return [build_apartment()]
