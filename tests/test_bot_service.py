"""Tests for bot service and formatting helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from bot.formatters import (
    format_criteria,
    format_monitor_status,
    format_saved_apartments,
    format_search_results,
    format_start_message,
)
from bot.keyboards import LIST_CALLBACK_DATA, REFINE_CALLBACK_DATA, build_search_followup_keyboard
from bot.monitoring import format_monitor_interval, parse_monitor_interval
from bot.service import ActiveCriteriaNotFoundError, SearchBotService


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

    stored_apartments: list[list[EnrichedApartment]] = []
    seen_links: list[tuple[int, int]] = []

    async def fake_upsert_apartments(session, *, apartments: list[EnrichedApartment]):
        del session
        stored_apartments.append(list(apartments))
        return [SimpleNamespace(id="apt-1")]

    async def fake_mark_seen(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session
        seen_links.append((user_id, len(apartments)))

    monkeypatch.setattr("bot.service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.service.replace_active_search_criteria", fake_replace)
    monkeypatch.setattr("bot.service.upsert_apartment_records", fake_upsert_apartments)
    monkeypatch.setattr("bot.service.mark_apartments_seen", fake_mark_seen)

    result = await service.run_search(
        telegram_user_id=77,
        username="tester",
        query="2-комнатная квартира в Алматы до 40 млн",
    )

    assert result.criteria.city == "Almaty"
    assert result.criteria.max_price_kzt == 40_000_000
    assert len(result.apartments) == 1
    assert stored_payloads[0]["city"] == "Almaty"
    assert stored_apartments[0][0].apartment.external_id == "900100"
    assert seen_links == [(123, 1)]
    assert session_factory.session.commit_calls == 2


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


@pytest.mark.asyncio
async def test_search_bot_service_loads_saved_apartments(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)

    async def fake_list_seen(session, *, telegram_user_id: int, limit: int):
        del session
        assert telegram_user_id == 77
        assert limit == 5
        return [build_apartment()]

    monkeypatch.setattr("bot.service.list_seen_apartments", fake_list_seen)

    apartments = await service.get_saved_apartments(telegram_user_id=77, limit=5)

    assert len(apartments) == 1
    assert apartments[0].apartment.title == "Bot test apartment"


@pytest.mark.asyncio
async def test_search_bot_service_refines_active_criteria(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)

    async def fake_get_record(session, *, telegram_user_id: int):
        del session
        assert telegram_user_id == 77
        return SimpleNamespace(
            criteria={
                "user_id": 77,
                "city": "Almaty",
                "deal_type": "sale",
                "property_type": "apartment",
                "min_price_kzt": 25_000_000,
                "max_price_kzt": 45_000_000,
                "rooms": [2, 3],
                "districts": ["Bostandyk"],
                "min_area_m2": 50.0,
                "max_area_m2": 80.0,
                "page_limit": 3,
            }
        )

    async def fake_upsert(session, *, telegram_user_id: int, username: str | None):
        del session
        assert telegram_user_id == 77
        assert username == "tester"
        return SimpleNamespace(id=123)

    stored_payloads: list[dict[str, object]] = []

    async def fake_replace(session, *, user_id: int, criteria_payload):
        del session
        assert user_id == 123
        stored_payloads.append(dict(criteria_payload))
        return SimpleNamespace()

    async def fake_upsert_apartments(session, *, apartments: list[EnrichedApartment]):
        del session
        return [SimpleNamespace(id="apt-1") for _ in apartments]

    async def fake_mark_seen(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session, user_id, apartments

    monkeypatch.setattr("bot.service.get_active_search_criteria_record", fake_get_record)
    monkeypatch.setattr("bot.service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.service.replace_active_search_criteria", fake_replace)
    monkeypatch.setattr("bot.service.upsert_apartment_records", fake_upsert_apartments)
    monkeypatch.setattr("bot.service.mark_apartments_seen", fake_mark_seen)

    result = await service.refine_search(
        telegram_user_id=77,
        username="tester",
        message="только 3 комнаты и до 35 млн",
    )

    assert result.criteria.city == "Almaty"
    assert result.criteria.min_price_kzt == 25_000_000
    assert result.criteria.max_price_kzt == 35_000_000
    assert result.criteria.rooms == [3]
    assert stored_payloads[0]["max_price_kzt"] == 35_000_000
    assert stored_payloads[0]["rooms"] == [3]
    assert session_factory.session.commit_calls == 2


@pytest.mark.asyncio
async def test_search_bot_service_refine_requires_active_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SearchBotService(
        session_factory=FakeSessionFactory(),
        search_runner=fake_search_runner,
    )

    async def fake_get_record(session, *, telegram_user_id: int):
        del session, telegram_user_id
        return None

    monkeypatch.setattr("bot.service.get_active_search_criteria_record", fake_get_record)

    with pytest.raises(ActiveCriteriaNotFoundError):
        await service.refine_search(
            telegram_user_id=77,
            username="tester",
            message="до 35 млн",
        )


@pytest.mark.asyncio
async def test_search_bot_service_reads_monitor_status(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)

    async def fake_get_monitor_record(session, *, telegram_user_id: int):
        del session
        assert telegram_user_id == 77
        return SimpleNamespace(is_enabled=True, interval_minutes=180)

    monkeypatch.setattr("bot.service.get_monitor_settings_record", fake_get_monitor_record)

    status = await service.get_monitor_status(telegram_user_id=77)

    assert status is not None
    assert status.enabled is True
    assert status.interval_minutes == 180


@pytest.mark.asyncio
async def test_search_bot_service_updates_monitor_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)

    async def fake_upsert(session, *, telegram_user_id: int, username: str | None):
        del session
        assert telegram_user_id == 77
        assert username == "tester"
        return SimpleNamespace(id=123)

    stored_changes: list[dict[str, object]] = []

    async def fake_upsert_monitor(
        session,
        *,
        user_id: int,
        is_enabled: bool | None = None,
        interval_minutes: int | None = None,
    ):
        del session
        stored_changes.append(
            {
                "user_id": user_id,
                "is_enabled": is_enabled,
                "interval_minutes": interval_minutes,
            }
        )
        return SimpleNamespace(
            is_enabled=is_enabled if is_enabled is not None else False,
            interval_minutes=interval_minutes if interval_minutes is not None else 360,
        )

    monkeypatch.setattr("bot.service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.service.upsert_monitor_settings", fake_upsert_monitor)

    enabled_status = await service.set_monitor_enabled(
        telegram_user_id=77,
        username="tester",
        enabled=True,
    )
    interval_status = await service.set_monitor_interval(
        telegram_user_id=77,
        username="tester",
        interval_minutes=720,
    )

    assert enabled_status.enabled is True
    assert enabled_status.interval_minutes == 360
    assert interval_status.enabled is False
    assert interval_status.interval_minutes == 720
    assert stored_changes == [
        {"user_id": 123, "is_enabled": True, "interval_minutes": None},
        {"user_id": 123, "is_enabled": None, "interval_minutes": 720},
    ]
    assert session_factory.session.commit_calls == 2


def test_monitor_interval_helpers_validate_and_format() -> None:
    assert parse_monitor_interval("30m") == 30
    assert parse_monitor_interval("6h") == 360
    assert parse_monitor_interval("1d") == 1440
    assert format_monitor_interval(360) == "6h"
    assert format_monitor_interval(45) == "45m"

    with pytest.raises(ValueError):
        parse_monitor_interval("10m")

    with pytest.raises(ValueError):
        parse_monitor_interval("abc")


def test_formatters_render_expected_content() -> None:
    service = SearchBotService(
        session_factory=FakeSessionFactory(),
        search_runner=fake_search_runner,
    )
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
    saved_text = format_saved_apartments([build_apartment()])
    monitor_text = format_monitor_status(service.get_default_monitor_status())
    empty_monitor_text = format_monitor_status(None)
    keyboard = build_search_followup_keyboard()

    assert "/search" in format_start_message()
    assert "/list" in format_start_message()
    assert "/refine" in format_start_message()
    assert "/monitor" in format_start_message()
    assert "Алматы" not in text
    assert "Город: Almaty" in text
    assert "40 000 000 KZT" in text
    assert "Bot test apartment" in results_text
    assert "Сохраненные квартиры" in saved_text
    assert "Статус мониторинга" in monitor_text
    assert "Мониторинг пока не настроен" in empty_monitor_text
    assert keyboard.inline_keyboard[0][0].callback_data == REFINE_CALLBACK_DATA
    assert keyboard.inline_keyboard[0][1].callback_data == LIST_CALLBACK_DATA


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
