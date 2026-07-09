"""Tests for bot service and formatting helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from agent.locations import LocationInputError
from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.models.score import ApartmentScore
from agent.nodes.intent_node import IntentNode, ParsedIntent
from agent.tools.krisha_parser import AntiBotBlockedError
from bot.formatters import (
    clean_listing_url,
    format_apartment_card,
    format_criteria,
    format_monitor_status,
    format_saved_apartments,
    format_search_results,
    format_start_message,
)
from bot.keyboards import (
    APT_REJECT_PREFIX,
    APT_SAVE_PREFIX,
    LIST_CALLBACK_DATA,
    REFINE_CALLBACK_DATA,
    SEARCH_MORE_CALLBACK_DATA,
    build_apartment_actions_keyboard,
    build_search_followup_keyboard,
)
from bot.monitoring import format_monitor_interval, parse_monitor_interval
from bot.service import (
    SEARCH_BLOCKED_MESSAGE,
    SEARCH_EXECUTION_ERROR_MESSAGE,
    ActiveCriteriaNotFoundError,
    CriteriaUnchangedError,
    NoPreferencesError,
    SearchBotService,
    SearchExecutionError,
)


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


@pytest.mark.asyncio
async def test_invalid_location_does_not_persist_or_run_search() -> None:
    calls = 0

    async def runner(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        return []

    session_factory = FakeSessionFactory()
    service = SearchBotService(
        session_factory=session_factory,
        intent_node=IntentNode(llm_parser_factory=lambda: None),
        search_runner=runner,
    )

    with pytest.raises(LocationInputError, match="не относится"):
        await service.run_search(
            telegram_user_id=42,
            username="tester",
            query="Астана, Бостандыкский район",
        )

    assert calls == 0
    assert session_factory.session.commit_calls == 0


def build_apartment(external_id: str = "900100") -> EnrichedApartment:
    return EnrichedApartment(
        apartment=Apartment(
            external_id=external_id,
            source="krisha",
            url=f"https://krisha.kz/a/show/{external_id}",
            title=f"Bot test apartment {external_id}",
            price_kzt=31_000_000,
            city="Almaty",
            rooms=2,
            area_m2=53.0,
            floor="5/9",
            photos=[f"https://photos.krisha.kz/{external_id}/1.jpg"],
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
        intent_node=IntentNode(llm_parser_factory=lambda: None),
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

    async def fake_feedback_map(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session, user_id, apartments
        return {}

    monkeypatch.setattr("bot.service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.service.replace_active_search_criteria", fake_replace)
    monkeypatch.setattr("bot.service.upsert_apartment_records", fake_upsert_apartments)
    monkeypatch.setattr("bot.service.mark_apartments_seen", fake_mark_seen)
    monkeypatch.setattr("bot.service.get_apartment_feedback_map", fake_feedback_map)

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

    async def fake_list_feedback(
        session,
        *,
        telegram_user_id: int,
        decision: str,
        limit: int,
    ):
        del session
        assert telegram_user_id == 77
        assert decision == "saved"
        assert limit == 5
        return [build_apartment()]

    monkeypatch.setattr("bot.feedback_service.list_feedback_apartments", fake_list_feedback)

    apartments = await service.get_saved_apartments(telegram_user_id=77, limit=5)

    assert len(apartments) == 1
    assert apartments[0].apartment.title == "Bot test apartment 900100"


@pytest.mark.asyncio
async def test_search_bot_service_counts_saved_apartments(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)

    async def fake_count(session, *, telegram_user_id: int, decision: str):
        del session
        assert telegram_user_id == 77
        assert decision == "saved"
        return 12

    monkeypatch.setattr("bot.feedback_service.count_feedback_apartments", fake_count)

    assert await service.count_saved_apartments(telegram_user_id=77) == 12


def _active_criteria_record() -> SimpleNamespace:
    return SimpleNamespace(
        criteria={
            "user_id": 77,
            "city": "Almaty",
            "deal_type": "sale",
            "property_type": "apartment",
        }
    )


@pytest.mark.asyncio
async def test_recommend_requires_active_criteria(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SearchBotService(
        session_factory=FakeSessionFactory(), search_runner=fake_search_runner
    )

    async def no_record(session, *, telegram_user_id: int):
        del session, telegram_user_id
        return None

    monkeypatch.setattr("bot.service.get_active_search_criteria_record", no_record)

    with pytest.raises(ActiveCriteriaNotFoundError):
        await service.recommend(telegram_user_id=77, username="tester")


@pytest.mark.asyncio
async def test_recommend_requires_saved_apartments(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SearchBotService(
        session_factory=FakeSessionFactory(), search_runner=fake_search_runner
    )

    async def record(session, *, telegram_user_id: int):
        del session, telegram_user_id
        return _active_criteria_record()

    async def no_feedback(session, *, telegram_user_id: int, decision: str, limit: int):
        del session, telegram_user_id, decision, limit
        return []

    async def upsert(session, *, telegram_user_id: int, username: str | None):
        del session, telegram_user_id, username
        return SimpleNamespace(id=123)

    monkeypatch.setattr("bot.service.get_active_search_criteria_record", record)
    monkeypatch.setattr("bot.recommendation_service.list_feedback_apartments", no_feedback)
    monkeypatch.setattr("bot.recommendation_service.upsert_telegram_user", upsert)

    with pytest.raises(NoPreferencesError):
        await service.recommend(telegram_user_id=77, username="tester")


@pytest.mark.asyncio
async def test_recommend_ranks_candidates_by_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SearchBotService(
        session_factory=FakeSessionFactory(), search_runner=fake_search_runner
    )

    async def record(session, *, telegram_user_id: int):
        del session, telegram_user_id
        return _active_criteria_record()

    async def feedback(session, *, telegram_user_id: int, decision: str, limit: int):
        del session, telegram_user_id, limit
        return [build_apartment()] if decision == "saved" else []

    async def upsert(session, *, telegram_user_id: int, username: str | None):
        del session, telegram_user_id, username
        return SimpleNamespace(id=123)

    async def upsert_apts(session, *, apartments):
        del session
        return [SimpleNamespace(id="apt-1")]

    async def feedback_map(session, *, user_id: int, apartments):
        del session, user_id, apartments
        return {}

    async def mark_seen(session, *, user_id: int, apartments):
        del session, user_id, apartments

    monkeypatch.setattr("bot.service.get_active_search_criteria_record", record)
    monkeypatch.setattr("bot.recommendation_service.list_feedback_apartments", feedback)
    monkeypatch.setattr("bot.recommendation_service.upsert_telegram_user", upsert)
    monkeypatch.setattr("bot.service.upsert_apartment_records", upsert_apts)
    monkeypatch.setattr("bot.service.get_apartment_feedback_map", feedback_map)
    monkeypatch.setattr("bot.service.mark_apartments_seen", mark_seen)

    result = await service.recommend(telegram_user_id=77, username="tester")

    assert len(result.recommendations) == 1
    rec = result.recommendations[0]
    assert rec.apartment.apartment.external_id == "900100"
    assert rec.reasons  # candidate matches the saved budget / rooms / area


@pytest.mark.asyncio
async def test_search_bot_service_lists_trashed(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SearchBotService(
        session_factory=FakeSessionFactory(), search_runner=fake_search_runner
    )

    async def fake_list_trashed(session, *, telegram_user_id: int, limit: int):
        del session
        assert telegram_user_id == 77
        assert limit == 7
        return [build_apartment(external_id="deleted-1")]

    async def fake_list_feedback(session, *, telegram_user_id: int, decision, limit: int):
        del session
        assert telegram_user_id == 77
        assert decision == "rejected"
        assert limit == 7
        return [build_apartment(external_id="rejected-1")]

    monkeypatch.setattr("bot.feedback_service.list_trashed_apartments", fake_list_trashed)
    monkeypatch.setattr("bot.feedback_service.list_feedback_apartments", fake_list_feedback)

    items = await service.get_trashed_apartments(telegram_user_id=77, limit=7)
    # Corzina merges rejected + deleted-from-saved, rejected first.
    assert [item.apartment.external_id for item in items] == ["rejected-1", "deleted-1"]


@pytest.mark.asyncio
async def test_search_bot_service_restores_apartment(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)

    async def fake_restore(session, *, telegram_user_id: int, external_id: str):
        del session
        assert telegram_user_id == 77
        assert external_id == "900100"
        return True

    monkeypatch.setattr("bot.feedback_service.restore_apartment_feedback", fake_restore)

    outcome = await service.restore_apartment(telegram_user_id=77, external_id="900100")
    assert outcome == "restored_to_saved"
    assert session_factory.session.commit_calls == 1


@pytest.mark.asyncio
async def test_search_bot_service_restore_unrejects(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)

    async def fake_restore(session, *, telegram_user_id: int, external_id: str):
        del session, telegram_user_id, external_id
        return False  # nothing soft-deleted to bring back

    async def fake_clear(session, *, telegram_user_id: int, external_id: str, decision):
        del session
        assert telegram_user_id == 77
        assert external_id == "900100"
        assert decision == "rejected"
        return True  # an active rejection was cleared

    monkeypatch.setattr("bot.feedback_service.restore_apartment_feedback", fake_restore)
    monkeypatch.setattr("bot.feedback_service.clear_apartment_feedback", fake_clear)

    outcome = await service.restore_apartment(telegram_user_id=77, external_id="900100")
    assert outcome == "unrejected"
    assert session_factory.session.commit_calls == 1


@pytest.mark.asyncio
async def test_search_bot_service_purges_trashed_apartment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)

    async def fake_tombstone(session, *, telegram_user_id: int, external_id: str):
        del session
        assert telegram_user_id == 77
        assert external_id == "900100"
        return True

    monkeypatch.setattr("bot.feedback_service.tombstone_apartment_feedback", fake_tombstone)

    assert await service.purge_trashed_apartment(telegram_user_id=77, external_id="900100") is True
    assert session_factory.session.commit_calls == 1


def test_build_trashed_item_keyboard_has_restore_and_open() -> None:
    from bot.keyboards import build_trashed_item_keyboard

    keyboard = build_trashed_item_keyboard("900100", "https://krisha.kz/a/show/900100")
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert any(button.url == "https://krisha.kz/a/show/900100" for button in buttons)
    assert any(button.callback_data == "trash:restore:900100" for button in buttons)
    assert any(button.callback_data == "trash:purge:900100" for button in buttons)


@pytest.mark.asyncio
async def test_search_bot_service_hides_saved_and_rejected_apartments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()

    async def fake_runner(
        criteria: SearchCriteria,
        *,
        thread_id: str,
        checkpoint_ns: str,
    ) -> list[EnrichedApartment]:
        assert criteria.city == "Almaty"
        assert thread_id == "telegram-user:77"
        assert checkpoint_ns == "telegram-search"
        return [
            build_apartment("900100"),
            build_apartment("900101"),
            build_apartment("900102"),
        ]

    service = SearchBotService(
        session_factory=session_factory,
        intent_node=IntentNode(llm_parser_factory=lambda: None),
        search_runner=fake_runner,
    )

    async def fake_upsert(session, *, telegram_user_id: int, username: str | None):
        del session, telegram_user_id, username
        return SimpleNamespace(id=123)

    async def fake_replace(session, *, user_id: int, criteria_payload):
        del session, criteria_payload
        assert user_id == 123
        return SimpleNamespace()

    seen_links: list[tuple[int, int]] = []
    stored_records = [
        SimpleNamespace(id="record-1"),
        SimpleNamespace(id="record-2"),
        SimpleNamespace(id="record-3"),
    ]

    async def fake_upsert_apartments(session, *, apartments: list[EnrichedApartment]):
        del session
        assert [item.apartment.external_id for item in apartments] == [
            "900100",
            "900101",
            "900102",
        ]
        return stored_records

    async def fake_mark_seen(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session
        seen_links.append((user_id, len(apartments)))

    async def fake_feedback_map(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session
        assert user_id == 123
        assert apartments == stored_records
        # record-1 has no feedback (kept); saved and rejected are both hidden.
        return {"record-2": "saved", "record-3": "rejected"}

    monkeypatch.setattr("bot.service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.service.replace_active_search_criteria", fake_replace)
    monkeypatch.setattr("bot.service.upsert_apartment_records", fake_upsert_apartments)
    monkeypatch.setattr("bot.service.mark_apartments_seen", fake_mark_seen)
    monkeypatch.setattr("bot.service.get_apartment_feedback_map", fake_feedback_map)

    result = await service.run_search(
        telegram_user_id=77,
        username="tester",
        query="2-комнатная квартира в Алматы до 40 млн",
    )

    assert [item.apartment.external_id for item in result.apartments] == ["900100"]
    assert seen_links == [(123, 3)]
    assert session_factory.session.commit_calls == 2


@pytest.mark.asyncio
async def test_search_bot_service_wraps_search_runner_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()

    async def failing_runner(
        criteria: SearchCriteria,
        *,
        thread_id: str,
        checkpoint_ns: str,
    ) -> list[EnrichedApartment]:
        del criteria, thread_id, checkpoint_ns
        raise PlaywrightTimeoutError("listing timeout")

    service = SearchBotService(
        session_factory=session_factory,
        intent_node=IntentNode(llm_parser_factory=lambda: None),
        search_runner=failing_runner,
    )

    async def fake_upsert(session, *, telegram_user_id: int, username: str | None):
        del session, telegram_user_id, username
        return SimpleNamespace(id=123)

    async def fake_replace(session, *, user_id: int, criteria_payload):
        del session, criteria_payload
        assert user_id == 123
        return SimpleNamespace()

    monkeypatch.setattr("bot.service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.service.replace_active_search_criteria", fake_replace)

    with pytest.raises(SearchExecutionError) as exc_info:
        await service.run_search(
            telegram_user_id=77,
            username="tester",
            query="2-комнатная квартира в Алматы до 40 млн",
        )

    assert str(exc_info.value) == SEARCH_EXECUTION_ERROR_MESSAGE
    assert isinstance(exc_info.value.__cause__, PlaywrightTimeoutError)
    assert session_factory.session.commit_calls == 1


@pytest.mark.asyncio
async def test_search_bot_service_reports_anti_bot_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()

    async def blocked_runner(
        criteria: SearchCriteria,
        *,
        thread_id: str,
        checkpoint_ns: str,
    ) -> list[EnrichedApartment]:
        del criteria, thread_id, checkpoint_ns
        raise AntiBotBlockedError("blocked")

    service = SearchBotService(
        session_factory=session_factory,
        intent_node=IntentNode(llm_parser_factory=lambda: None),
        search_runner=blocked_runner,
    )

    async def fake_upsert(session, *, telegram_user_id: int, username: str | None):
        del session, telegram_user_id, username
        return SimpleNamespace(id=123)

    async def fake_replace(session, *, user_id: int, criteria_payload):
        del session, user_id, criteria_payload
        return SimpleNamespace()

    monkeypatch.setattr("bot.service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.service.replace_active_search_criteria", fake_replace)

    with pytest.raises(SearchExecutionError) as exc_info:
        await service.run_search(
            telegram_user_id=77,
            username="tester",
            query="2-комнатная квартира в Алматы до 40 млн",
        )

    # Block must yield the dedicated message, distinct from the generic failure.
    assert exc_info.value.user_message == SEARCH_BLOCKED_MESSAGE
    assert SEARCH_BLOCKED_MESSAGE != SEARCH_EXECUTION_ERROR_MESSAGE
    assert isinstance(exc_info.value.__cause__, AntiBotBlockedError)


@pytest.mark.asyncio
async def test_search_bot_service_records_save_and_reject_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(
        session_factory=session_factory,
        intent_node=IntentNode(llm_parser_factory=lambda: None),
        search_runner=fake_search_runner,
    )

    async def fake_upsert(session, *, telegram_user_id: int, username: str | None):
        del session
        assert telegram_user_id == 77
        assert username == "tester"
        return SimpleNamespace(id=123)

    async def fake_list_apartments(session, *, urls: list[str]):
        del session
        assert urls == [
            "https://krisha.kz/a/show/900100",
            "https://krisha.kz/a/show/900101",
        ]
        return [
            SimpleNamespace(id="record-1", url=urls[0]),
            SimpleNamespace(id="record-2", url=urls[1]),
        ]

    decisions: list[tuple[int, str, int]] = []

    async def fake_upsert_feedback(session, *, user_id: int, apartments, decision: str):
        del session
        decisions.append((user_id, decision, len(apartments)))
        return []

    monkeypatch.setattr("bot.feedback_service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.feedback_service.list_apartment_records_by_urls", fake_list_apartments)
    monkeypatch.setattr("bot.feedback_service.upsert_apartment_feedback", fake_upsert_feedback)

    saved_count = await service.save_apartments(
        telegram_user_id=77,
        username="tester",
        apartment_urls=[
            "https://krisha.kz/a/show/900100",
            "https://krisha.kz/a/show/900101",
            "https://krisha.kz/a/show/900100",
        ],
    )
    rejected_count = await service.reject_apartments(
        telegram_user_id=77,
        username="tester",
        apartment_urls=[
            "https://krisha.kz/a/show/900100",
            "https://krisha.kz/a/show/900101",
        ],
    )

    assert saved_count == 2
    assert rejected_count == 2
    assert decisions == [
        (123, "saved", 2),
        (123, "rejected", 2),
    ]
    assert session_factory.session.commit_calls == 2


@pytest.mark.asyncio
async def test_search_bot_service_syncs_saved_apartments_to_notion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()
    notion_calls: list[tuple[str, str | None]] = []

    class FakeNotionSync:
        async def sync_apartment(
            self,
            apartment: EnrichedApartment,
            *,
            page_id: str | None = None,
        ) -> str:
            notion_calls.append((apartment.apartment.external_id, page_id))
            if apartment.apartment.external_id == "900100":
                return "page-100"
            return "page-101"

    service = SearchBotService(
        session_factory=session_factory,
        search_runner=fake_search_runner,
        notion_sync=FakeNotionSync(),
    )

    apartment_id_one = uuid.uuid4()
    apartment_id_two = uuid.uuid4()
    synced_updates: list[dict[uuid.UUID, str]] = []

    async def fake_upsert(session, *, telegram_user_id: int, username: str | None):
        del session
        assert telegram_user_id == 77
        assert username == "tester"
        return SimpleNamespace(id=123)

    async def fake_list_apartments(session, *, urls: list[str]):
        del session
        assert urls == [
            "https://krisha.kz/a/show/900100",
            "https://krisha.kz/a/show/900101",
        ]
        return [
            SimpleNamespace(
                id=apartment_id_one,
                url=urls[0],
                payload=build_apartment("900100").model_dump(mode="json"),
            ),
            SimpleNamespace(
                id=apartment_id_two,
                url=urls[1],
                payload=build_apartment("900101").model_dump(mode="json"),
            ),
        ]

    async def fake_upsert_feedback(session, *, user_id: int, apartments, decision: str):
        del session
        assert user_id == 123
        assert decision == "saved"
        return [
            SimpleNamespace(apartment_id=apartment_id_one, notion_page_id=None),
            SimpleNamespace(apartment_id=apartment_id_two, notion_page_id="page-existing"),
        ]

    async def fake_update_sync(session, *, user_id: int, synced_pages, synced_at):
        del session
        assert user_id == 123
        assert synced_at.tzinfo is not None
        synced_updates.append(dict(synced_pages))
        return []

    monkeypatch.setattr("bot.feedback_service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.feedback_service.list_apartment_records_by_urls", fake_list_apartments)
    monkeypatch.setattr("bot.feedback_service.upsert_apartment_feedback", fake_upsert_feedback)
    monkeypatch.setattr(
        "bot.feedback_service.update_apartment_feedback_notion_sync", fake_update_sync
    )

    saved_count = await service.save_apartments(
        telegram_user_id=77,
        username="tester",
        apartment_urls=[
            "https://krisha.kz/a/show/900100",
            "https://krisha.kz/a/show/900101",
        ],
    )

    assert saved_count == 2
    assert notion_calls == [
        ("900100", None),
        ("900101", "page-existing"),
    ]
    assert synced_updates == [
        {
            apartment_id_one: "page-100",
            apartment_id_two: "page-101",
        }
    ]
    assert session_factory.session.commit_calls == 2


@pytest.mark.asyncio
async def test_search_bot_service_refines_active_criteria(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(
        session_factory=session_factory,
        intent_node=IntentNode(llm_parser_factory=lambda: None),
        search_runner=fake_search_runner,
    )

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

    async def fake_feedback_map(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session, user_id, apartments
        return {}

    monkeypatch.setattr("bot.service.get_active_search_criteria_record", fake_get_record)
    monkeypatch.setattr("bot.service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.service.replace_active_search_criteria", fake_replace)
    monkeypatch.setattr("bot.service.upsert_apartment_records", fake_upsert_apartments)
    monkeypatch.setattr("bot.service.mark_apartments_seen", fake_mark_seen)
    monkeypatch.setattr("bot.service.get_apartment_feedback_map", fake_feedback_map)

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
        intent_node=IntentNode(llm_parser_factory=lambda: None),
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


class _FakeIntentNode:
    """Intent node stub with controllable refine + parse_with_metadata."""

    def __init__(self, refined: SearchCriteria, parsed: ParsedIntent) -> None:
        self._refined = refined
        self._parsed = parsed

    async def refine(self, *, criteria: SearchCriteria, message: str) -> SearchCriteria:
        del criteria, message
        return self._refined

    async def parse_with_metadata(self, *, user_id: int, message: str) -> ParsedIntent:
        del user_id, message
        return self._parsed


def _active_criteria() -> SearchCriteria:
    return SearchCriteria(
        user_id=77,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        max_price_kzt=45_000_000,
        rooms=[2],
        page_limit=3,
    )


def _patch_refine_db(monkeypatch: pytest.MonkeyPatch, active: SearchCriteria) -> None:
    async def fake_get_record(session, *, telegram_user_id: int):
        del session, telegram_user_id
        return SimpleNamespace(criteria=active.model_dump(mode="json"))

    async def fake_upsert(session, *, telegram_user_id: int, username: str | None):
        del session, telegram_user_id, username
        return SimpleNamespace(id=123)

    async def fake_replace(session, *, user_id: int, criteria_payload):
        del session, user_id, criteria_payload

    async def fake_upsert_apartments(session, *, apartments: list[EnrichedApartment]):
        del session
        return [SimpleNamespace(id="apt") for _ in apartments]

    async def fake_mark_seen(session, *, user_id: int, apartments):
        del session, user_id, apartments

    async def fake_feedback_map(session, *, user_id: int, apartments):
        del session, user_id, apartments
        return {}

    monkeypatch.setattr("bot.service.get_active_search_criteria_record", fake_get_record)
    monkeypatch.setattr("bot.service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.service.replace_active_search_criteria", fake_replace)
    monkeypatch.setattr("bot.service.upsert_apartment_records", fake_upsert_apartments)
    monkeypatch.setattr("bot.service.mark_apartments_seen", fake_mark_seen)
    monkeypatch.setattr("bot.service.get_apartment_feedback_map", fake_feedback_map)


@pytest.mark.asyncio
async def test_refine_reruns_when_message_restates_same_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # User re-types a full query identical to the active criteria while in refine
    # mode: no delta, but it's a recognizable search -> run it, don't error.
    active = _active_criteria()
    parsed = ParsedIntent(criteria=active, defaulted_city=False)
    service = SearchBotService(
        session_factory=FakeSessionFactory(),
        intent_node=_FakeIntentNode(active, parsed),
        search_runner=fake_search_runner,
    )
    _patch_refine_db(monkeypatch, active)

    result = await service.refine_search(
        telegram_user_id=77, username="tester", message="2-комнатная в Алматы до 45 млн"
    )

    assert result.criteria == active
    assert len(result.apartments) == 1


@pytest.mark.asyncio
async def test_refine_rejects_unrecognized_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # No delta AND no recognizable criteria (e.g. "привет") -> keep refine hint.
    active = _active_criteria()
    empty = SearchCriteria(
        user_id=77, city="Almaty", deal_type="sale", property_type="apartment", page_limit=3
    )
    parsed = ParsedIntent(criteria=empty, defaulted_city=True)
    service = SearchBotService(
        session_factory=FakeSessionFactory(),
        intent_node=_FakeIntentNode(active, parsed),
        search_runner=fake_search_runner,
    )
    _patch_refine_db(monkeypatch, active)

    with pytest.raises(CriteriaUnchangedError):
        await service.refine_search(telegram_user_id=77, username="tester", message="привет")


@pytest.mark.asyncio
async def test_set_active_city_resolves_typo_and_clears_districts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = SearchCriteria(
        user_id=77, city="Almaty", deal_type="sale", property_type="apartment",
        districts=["Medeu"], rooms=[2], page_limit=3,
    )
    service = SearchBotService(
        session_factory=FakeSessionFactory(), search_runner=fake_search_runner
    )
    _patch_refine_db(monkeypatch, active)

    updated, ok = await service.set_active_city(
        telegram_user_id=77, username="t", city_text="Астанна"  # typo -> Astana
    )

    assert ok is True
    assert updated.city == "Astana"
    assert updated.districts is None  # districts are city-specific -> cleared


@pytest.mark.asyncio
async def test_set_active_city_rejects_unknown_city(monkeypatch: pytest.MonkeyPatch) -> None:
    active = _active_criteria()
    service = SearchBotService(
        session_factory=FakeSessionFactory(), search_runner=fake_search_runner
    )
    _patch_refine_db(monkeypatch, active)

    updated, ok = await service.set_active_city(
        telegram_user_id=77, username="t", city_text="Москва"
    )

    assert ok is False
    assert updated.city == "Almaty"  # unchanged


@pytest.mark.asyncio
async def test_set_active_deal_and_district(monkeypatch: pytest.MonkeyPatch) -> None:
    active = _active_criteria()
    service = SearchBotService(
        session_factory=FakeSessionFactory(), search_runner=fake_search_runner
    )
    _patch_refine_db(monkeypatch, active)

    # sale -> rent: the purchase budget (45M) makes no sense for rent -> cleared
    deal, budget_reset = await service.set_active_deal_type(
        telegram_user_id=77, username="t", deal_type="rent", rent_period="monthly"
    )
    assert deal.deal_type == "rent"
    assert deal.rent_period == "monthly"
    assert budget_reset is True
    assert deal.min_price_kzt is None and deal.max_price_kzt is None

    # same deal type as stored (fixture is sale): no change -> budget kept
    same, budget_reset_same = await service.set_active_deal_type(
        telegram_user_id=77, username="t", deal_type="sale"
    )
    assert budget_reset_same is False
    assert same.max_price_kzt == 45_000_000
    assert same.rent_period is None  # sale never carries a rent period

    with_district = await service.set_active_district(
        telegram_user_id=77, username="t", district="Medeu"
    )
    assert with_district.districts == ["Medeu"]

    cleared = await service.set_active_district(
        telegram_user_id=77, username="t", district=None
    )
    assert cleared.districts is None


@pytest.mark.asyncio
async def test_toggle_active_owner_only(monkeypatch: pytest.MonkeyPatch) -> None:
    active = _active_criteria()  # owner_only defaults to False
    service = SearchBotService(
        session_factory=FakeSessionFactory(), search_runner=fake_search_runner
    )
    _patch_refine_db(monkeypatch, active)

    updated = await service.toggle_active_owner_only(telegram_user_id=77, username="t")
    assert updated.owner_only is True


def test_refine_menu_keyboard_shows_owner_toggle_state() -> None:
    from bot.keyboards import REFINE_TOGGLE_OWNER, build_refine_menu_keyboard

    def owner_button(owner_only: bool) -> str:
        keyboard = build_refine_menu_keyboard("Almaty", owner_only=owner_only)
        return next(
            b.text
            for row in keyboard.inline_keyboard
            for b in row
            if b.callback_data == REFINE_TOGGLE_OWNER
        )

    assert "✅" in owner_button(True)
    assert "—" in owner_button(False)


def test_format_apartment_card_market_features_description() -> None:
    from bot.formatters import BatchPriceStats

    def make(**over: object) -> EnrichedApartment:
        base = build_apartment()
        return base.model_copy(
            update={"apartment": base.apartment.model_copy(update=over)}
        )

    # krisha market verdict preferred over the batch line; avg derived from ₸/м²
    item = make(
        price_kzt=35_343_990, area_m2=55.5, market_diff_percent=-9.2,
        build_year=2019, building_type="монолитный", ceiling_height_m=2.7,
        furnished="частично",
        description="Тёплая, свежий ремонт, распашонка. Торг. " * 6,  # noqa: RUF001
    )
    stats = BatchPriceStats(avg_price_per_m2=700_000, count=6)
    card = format_apartment_card(item, index=1, price_stats=stats)

    assert "🏙 на 9% дешевле рынка города" in card
    assert "среднего за м²" not in card  # batch line suppressed when krisha present
    assert "🏗 2019 · монолитный · потолки 2.7 м · 🛋 частично" in card
    assert "📝 Тёплая, свежий ремонт" in card
    assert card.count("📝") == 1  # description is a single snippet line

    # no krisha verdict -> fall back to the batch line
    fallback = format_apartment_card(
        make(price_kzt=41_000_000, area_m2=44.0), index=1, price_stats=stats
    )
    assert "среднего за м²" in fallback
    assert "🏙" not in fallback


def test_format_apartment_card_shows_poster() -> None:
    owner_item = build_apartment()
    owner_item = owner_item.model_copy(
        update={"apartment": owner_item.apartment.model_copy(update={"posted_by": "owner"})}
    )
    assert "👤 От хозяина" in format_apartment_card(owner_item, index=1)

    agent_item = build_apartment()
    agent_item = agent_item.model_copy(
        update={
            "apartment": agent_item.apartment.model_copy(
                update={"posted_by": "agent", "agency_name": "Top City"}
            )
        }
    )
    assert "🏢 От риелтора (Top City)" in format_apartment_card(agent_item, index=1)

    # unknown -> no poster line at all
    plain_card = format_apartment_card(build_apartment(), index=1)
    assert "От хозяина" not in plain_card and "От риелтора" not in plain_card


def test_format_apartment_card_shows_days_on_market() -> None:
    from bot.formatters import _plural_days

    def with_published(published: datetime) -> EnrichedApartment:
        base = build_apartment()
        return base.model_copy(
            update={"apartment": base.apartment.model_copy(update={"published_at": published})}
        )

    # fixed clock via now= so the assertions are deterministic (no wall time)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    stale = with_published(now - timedelta(days=120))
    assert "висит 120 дней" in format_apartment_card(stale, index=1, now=now)

    fresh = with_published(now)
    assert "🆕 сегодня" in format_apartment_card(fresh, index=1, now=now)

    # no publish date -> no dedicated line at all
    assert "висит" not in format_apartment_card(build_apartment(), index=1)

    # Russian plural agreement for «день»
    assert _plural_days(1) == "день"
    assert _plural_days(3) == "дня"
    assert _plural_days(5) == "дней"
    assert _plural_days(21) == "день"
    assert _plural_days(112) == "дней"


def test_format_criteria_shows_owner_only() -> None:
    criteria = _active_criteria().model_copy(update={"owner_only": True})
    assert "Только от хозяина: да" in format_criteria(criteria)
    assert "от хозяина" not in format_criteria(_active_criteria())


def test_format_criteria_shows_rent_period() -> None:
    rent = _active_criteria().model_copy(update={"deal_type": "rent"})
    assert "Сделка: аренда (помесячно)" in format_criteria(rent)
    daily = rent.model_copy(update={"rent_period": "daily"})
    assert "Сделка: аренда (посуточно)" in format_criteria(daily)
    hourly = rent.model_copy(update={"rent_period": "hourly"})
    assert "Сделка: аренда (по часам)" in format_criteria(hourly)
    assert "Сделка: покупка" in format_criteria(_active_criteria())


def test_refine_deal_keyboard_is_two_step_with_periods() -> None:
    from bot.keyboards import (
        REFINE_FIELD_PREFIX,
        REFINE_SET_DEAL_PREFIX,
        REFINE_SET_PERIOD_PREFIX,
        build_refine_deal_keyboard,
        build_refine_menu_keyboard,
        build_refine_rent_period_keyboard,
    )

    # step 1: only Купить / Снять
    deal_datas = [
        b.callback_data
        for row in build_refine_deal_keyboard().inline_keyboard
        for b in row
    ]
    assert f"{REFINE_SET_DEAL_PREFIX}sale" in deal_datas
    assert f"{REFINE_SET_DEAL_PREFIX}rent" in deal_datas
    assert not any("rent:" in (d or "") for d in deal_datas)

    # step 2 (after Снять): the krisha rent terms
    period_datas = [
        b.callback_data
        for row in build_refine_rent_period_keyboard().inline_keyboard
        for b in row
    ]
    assert f"{REFINE_SET_PERIOD_PREFIX}monthly" in period_datas
    assert f"{REFINE_SET_PERIOD_PREFIX}daily" in period_datas
    assert f"{REFINE_SET_PERIOD_PREFIX}hourly" in period_datas

    # the menu shows the rent-term row only while the deal is a rent
    def menu_datas(is_rent: bool) -> list[str]:
        keyboard = build_refine_menu_keyboard("Almaty", is_rent=is_rent)
        return [b.callback_data for row in keyboard.inline_keyboard for b in row]

    assert f"{REFINE_FIELD_PREFIX}period" in menu_datas(True)
    assert f"{REFINE_FIELD_PREFIX}period" not in menu_datas(False)


@pytest.mark.asyncio
async def test_apply_refinement_value_merges_typed_field(monkeypatch: pytest.MonkeyPatch) -> None:
    active = _active_criteria()
    refined = active.model_copy(update={"max_price_kzt": 40_000_000})
    parsed = ParsedIntent(criteria=refined, defaulted_city=False)
    service = SearchBotService(
        session_factory=FakeSessionFactory(),
        intent_node=_FakeIntentNode(refined, parsed),
        search_runner=fake_search_runner,
    )
    _patch_refine_db(monkeypatch, active)

    result = await service.apply_refinement_value(
        telegram_user_id=77, username="t", message="до 40 млн"
    )

    assert result.max_price_kzt == 40_000_000


def test_refine_menu_keyboard_shows_district_only_when_city_has_them() -> None:
    from bot.keyboards import (
        REFINE_FIELD_PREFIX,
        REFINE_RUN,
        build_refine_menu_keyboard,
    )

    def datas(city: str) -> list[str]:
        return [
            b.callback_data
            for row in build_refine_menu_keyboard(city).inline_keyboard
            for b in row
        ]

    almaty = datas("Almaty")
    assert f"{REFINE_FIELD_PREFIX}district" in almaty
    assert f"{REFINE_FIELD_PREFIX}city" in almaty
    assert REFINE_RUN in almaty

    assert f"{REFINE_FIELD_PREFIX}district" not in datas("Konaev")  # Konaev has no districts


def test_refine_district_keyboard_lists_city_districts_and_clear() -> None:
    from bot.keyboards import (
        REFINE_DISTRICT_CLEAR,
        REFINE_SET_DISTRICT_PREFIX,
        build_refine_district_keyboard,
    )

    datas = [
        b.callback_data
        for row in build_refine_district_keyboard("Almaty").inline_keyboard
        for b in row
    ]
    assert f"{REFINE_SET_DISTRICT_PREFIX}Medeu" in datas
    assert f"{REFINE_SET_DISTRICT_PREFIX}{REFINE_DISTRICT_CLEAR}" in datas


@pytest.mark.asyncio
async def test_search_bot_service_reruns_active_criteria(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)

    stored_criteria = {
        "user_id": 77,
        "city": "Almaty",
        "deal_type": "sale",
        "property_type": "apartment",
        "max_price_kzt": 45_000_000,
        "rooms": [2],
        "districts": ["Medeu"],
        "page_limit": 3,
    }

    async def fake_get_record(session, *, telegram_user_id: int):
        del session
        assert telegram_user_id == 77
        return SimpleNamespace(criteria=stored_criteria)

    async def fake_upsert(session, *, telegram_user_id: int, username: str | None):
        del session, telegram_user_id, username
        return SimpleNamespace(id=123)

    stored_payloads: list[dict[str, object]] = []

    async def fake_replace(session, *, user_id: int, criteria_payload):
        del session, user_id
        stored_payloads.append(dict(criteria_payload))
        return SimpleNamespace()

    async def fake_upsert_apartments(session, *, apartments: list[EnrichedApartment]):
        del session
        return [SimpleNamespace(id="apt-1") for _ in apartments]

    async def fake_mark_seen(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session, user_id, apartments

    async def fake_feedback_map(session, *, user_id: int, apartments: list[SimpleNamespace]):
        del session, user_id, apartments
        return {}

    monkeypatch.setattr("bot.service.get_active_search_criteria_record", fake_get_record)
    monkeypatch.setattr("bot.service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.service.replace_active_search_criteria", fake_replace)
    monkeypatch.setattr("bot.service.upsert_apartment_records", fake_upsert_apartments)
    monkeypatch.setattr("bot.service.mark_apartments_seen", fake_mark_seen)
    monkeypatch.setattr("bot.service.get_apartment_feedback_map", fake_feedback_map)

    result = await service.rerun_active_search(telegram_user_id=77, username="tester")

    # Re-runs the stored criteria unchanged (the "next batch"); dedup lives in the
    # search runner, so here we just confirm the same criteria drive the search.
    assert result.criteria.city == "Almaty"
    assert result.criteria.rooms == [2]
    assert result.criteria.districts == ["Medeu"]
    assert len(result.apartments) == 1
    assert stored_payloads[0]["rooms"] == [2]
    assert stored_payloads[0]["districts"] == ["Medeu"]


@pytest.mark.asyncio
async def test_search_bot_service_rerun_requires_active_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SearchBotService(
        session_factory=FakeSessionFactory(), search_runner=fake_search_runner
    )

    async def fake_get_record(session, *, telegram_user_id: int):
        del session, telegram_user_id
        return None

    monkeypatch.setattr("bot.service.get_active_search_criteria_record", fake_get_record)

    with pytest.raises(ActiveCriteriaNotFoundError):
        await service.rerun_active_search(telegram_user_id=77, username="tester")


@pytest.mark.asyncio
async def test_search_bot_service_reads_monitor_status(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)

    async def fake_get_monitor_record(session, *, telegram_user_id: int):
        del session
        assert telegram_user_id == 77
        return SimpleNamespace(is_enabled=True, interval_minutes=180)

    monkeypatch.setattr("bot.monitor_service.get_monitor_settings_record", fake_get_monitor_record)

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

    monkeypatch.setattr("bot.monitor_service.upsert_telegram_user", fake_upsert)
    monkeypatch.setattr("bot.monitor_service.upsert_monitor_settings", fake_upsert_monitor)

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
    # Links moved to the "🌐 Открыть на Krisha" button — captions carry no raw URL.
    assert "krisha.kz/a/show" not in results_text
    assert "31 000 000 ₸" in results_text
    assert "Сохраненные квартиры" in saved_text
    assert "Статус мониторинга" in monitor_text
    assert "Мониторинг пока не настроен" in empty_monitor_text
    assert keyboard.inline_keyboard[0][0].callback_data == SEARCH_MORE_CALLBACK_DATA
    assert keyboard.inline_keyboard[1][0].callback_data == REFINE_CALLBACK_DATA
    assert keyboard.inline_keyboard[1][1].callback_data == LIST_CALLBACK_DATA

    actions = build_apartment_actions_keyboard(
        "1013149871", "https://krisha.kz/a/show/1013149871"
    )
    assert actions.inline_keyboard[0][0].url == "https://krisha.kz/a/show/1013149871"
    assert actions.inline_keyboard[1][0].callback_data == f"{APT_SAVE_PREFIX}1013149871"
    assert actions.inline_keyboard[1][1].callback_data == f"{APT_REJECT_PREFIX}1013149871"

    # without a url the link row is simply absent
    actions_no_url = build_apartment_actions_keyboard("1013149871")
    assert actions_no_url.inline_keyboard[0][0].callback_data == f"{APT_SAVE_PREFIX}1013149871"


@pytest.mark.asyncio
async def test_search_bot_service_deletes_saved_apartment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = FakeSessionFactory()
    service = SearchBotService(session_factory=session_factory, search_runner=fake_search_runner)
    seen: list[tuple[int, str]] = []

    async def fake_delete(session, *, telegram_user_id: int, external_id: str, decision="saved"):
        del session, decision
        seen.append((telegram_user_id, external_id))
        return external_id == "900100"

    monkeypatch.setattr("bot.feedback_service.delete_apartment_feedback", fake_delete)

    assert await service.delete_saved_apartment(telegram_user_id=77, external_id="900100") is True
    assert await service.delete_saved_apartment(telegram_user_id=77, external_id="000") is False
    assert seen == [(77, "900100"), (77, "000")]
    assert session_factory.session.commit_calls == 2


def test_clean_listing_url_strips_tracking_query() -> None:
    assert (
        clean_listing_url("https://krisha.kz/a/show/1?srchid=abc&srchpos=2#frag")
        == "https://krisha.kz/a/show/1"
    )
    # mobile host is normalized to the canonical desktop one
    assert (
        clean_listing_url("https://m.krisha.kz/a/show/1012905312")
        == "https://krisha.kz/a/show/1012905312"
    )


def test_format_apartment_card_price_vs_batch_and_metro_zero() -> None:
    def make(price: int, area: float, metro: int | None) -> EnrichedApartment:
        return EnrichedApartment(
            apartment=Apartment(
                external_id="1", source="krisha", url="https://krisha.kz/a/show/1",
                title="t", price_kzt=price, city="Almaty", rooms=2, area_m2=area,
                photos=[],
            ),
            nearby_schools=7,
            nearby_parks=36,
            nearby_metro=metro,
            nearby_school_m=559,
            nearby_park_m=51,
        )

    from bot.formatters import BatchPriceStats

    stats = BatchPriceStats(avg_price_per_m2=800_000, count=6)

    # ~932K/m² vs batch avg 800K/m² -> "дороже"; metro true zero -> words, not "0"
    card = format_apartment_card(make(41_000_000, 44.0, 0), index=3, price_stats=stats)
    assert "дороже среднего за м²" in card
    # names what it compares against: the 6-listing batch and its average
    assert "по 6 вариантам (среднее 800 000 ₸/м²)" in card
    assert "🚇 метро: нет рядом (2 км+)" in card
    assert "метро: 0" not in card

    # cheaper than batch average -> "дешевле"
    card = format_apartment_card(make(30_000_000, 44.0, 1), index=1, price_stats=stats)
    assert "дешевле среднего за м²" in card

    # within ±3% -> neutral wording; no stats -> no comparison line at all
    card = format_apartment_card(make(35_200_000, 44.0, 1), index=1, price_stats=stats)
    assert "на уровне среднего по 6 вариантам" in card
    card = format_apartment_card(make(41_000_000, 44.0, 1), index=1)
    assert "📊" not in card


def test_format_apartment_card_is_clean_and_structured() -> None:
    item = EnrichedApartment(
        apartment=Apartment(
            external_id="900100",
            source="krisha",
            url="https://krisha.kz/a/show/900100?srchid=abc&srchtype=filter",
            title="1-комнатная квартира · 40 м²  Тауелсиздик 34/10",
            price_kzt=31_000_000,
            city="Almaty",
            district="Есильский район",
            rooms=2,
            area_m2=53.0,
            floor="5/9",
            photos=[],
        ),
        nearby_schools=5,
        nearby_parks=3,
        nearby_metro=1,
        score=ApartmentScore(
            score=85.0,
            reasons=["рядом школы", "хорошая цена"],
            recommendation="consider",
        ),
    )

    card = format_apartment_card(item, index=1)

    assert "2-комнатная · 53 м² · этаж 5/9" in card
    assert "💰 31 000 000 ₸" in card
    assert "₸/м²" in card  # price per square meter shown
    assert "Almaty" in card
    assert "Есильский" in card
    assert "85/100" in card
    assert "школы: 5" in card
    assert "рядом школы" in card  # score reasons included
    # The link lives on the "🌐 Открыть на Krisha" button, not in the caption.
    assert "🔗" not in card and "https://" not in card
    assert "<b>" not in card and "href=" not in card  # plain text, renders without parse_mode


def test_build_saved_item_keyboard_includes_open_link() -> None:
    from bot.keyboards import build_saved_item_keyboard

    with_url = build_saved_item_keyboard("900100", "https://krisha.kz/a/show/900100")
    buttons = [button for row in with_url.inline_keyboard for button in row]
    assert any(button.url == "https://krisha.kz/a/show/900100" for button in buttons)
    assert any(button.callback_data == "saved:del:900100" for button in buttons)

    # No URL -> only the delete button (e.g. a listing without a stored link).
    flat = [button for row in build_saved_item_keyboard("900100").inline_keyboard for button in row]
    assert len(flat) == 1
    assert flat[0].url is None


async def fake_search_runner(
    criteria: SearchCriteria,
    *,
    thread_id: str,
    checkpoint_ns: str,
    dedup_namespace: str = "search",
) -> list[EnrichedApartment]:
    assert criteria.city == "Almaty"
    assert thread_id == "telegram-user:77"
    assert checkpoint_ns == "telegram-search"
    assert dedup_namespace in {"search", "foryou"}
    return [build_apartment()]


def test_caption_budget_lets_description_fill_the_card() -> None:
    from bot.formatters import TELEGRAM_PHOTO_CAPTION_LIMIT, telegram_text_length

    def with_description(text: str) -> EnrichedApartment:
        base = build_apartment()
        return base.model_copy(
            update={"apartment": base.apartment.model_copy(update={"description": text})}
        )

    # a description longer than the old 160-char teaser but fitting the caption
    medium = "Просторная квартира с видом на горы. " * 12  # ~440 chars  # noqa: RUF001
    card = format_apartment_card(
        with_description(medium), index=1, caption_budget=TELEGRAM_PHOTO_CAPTION_LIMIT
    )
    assert medium.strip() in card  # included WHOLE, not cut at 160
    assert telegram_text_length(card) <= TELEGRAM_PHOTO_CAPTION_LIMIT

    # a huge description gets everything that fits, capped at the budget
    huge = "Очень длинное описание квартиры с деталями. 🏡 " * 60  # noqa: RUF001
    card = format_apartment_card(
        with_description(huge), index=1, caption_budget=TELEGRAM_PHOTO_CAPTION_LIMIT
    )
    assert telegram_text_length(card) <= TELEGRAM_PHOTO_CAPTION_LIMIT
    assert card.rstrip().endswith("…")
    # and it used the space: far more than the legacy teaser made it in
    snippet_line = next(line for line in card.splitlines() if line.startswith("📝"))
    assert len(snippet_line) > 300

    # without a budget (multi-card text lists) the 160-char teaser stays
    legacy = format_apartment_card(with_description(huge), index=1)
    legacy_snippet = next(line for line in legacy.splitlines() if line.startswith("📝"))
    assert len(legacy_snippet) <= 170


def test_card_prefers_ai_description_summary_over_raw_text() -> None:
    raw = "✅ Проверенная квартира ЖК JAR-JAR ... длинная риелторская вода ... " * 20
    base = build_apartment()
    with_description = base.model_copy(
        update={"apartment": base.apartment.model_copy(update={"description": raw})}
    )

    summarized = with_description.model_copy(
        update={
            "score": ApartmentScore(
                score=80,
                reasons=["цена ниже среднего"],
                recommendation="consider",
                description_summary="ЖК JAR-JAR, сдача Q2 2026, чистовая отделка, торг.",
            )
        }
    )
    card = format_apartment_card(summarized, index=1)
    assert "📝 ЖК JAR-JAR, сдача Q2 2026, чистовая отделка, торг." in card
    assert "риелторская вода" not in card

    # scored but without a summary -> raw description still shown (truncated)
    unsummarized = with_description.model_copy(
        update={
            "score": ApartmentScore(
                score=80, reasons=["цена ниже среднего"], recommendation="consider"
            )
        }
    )
    fallback = format_apartment_card(unsummarized, index=1)
    assert "📝 ✅ Проверенная квартира ЖК JAR-JAR" in fallback
