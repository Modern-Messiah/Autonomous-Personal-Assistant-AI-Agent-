"""Tests for supervisor-style dialog agent."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from bot.dialog_agent import DialogAgent, DialogIntentNode
from bot.service import MonitorStatus, SearchExecution


class DummyDialogService:
    """Service stub used by dialog agent tests."""

    def __init__(self, *, active_criteria: SearchCriteria | None = None) -> None:
        self.active_criteria = active_criteria
        self.search_queries: list[str] = []
        self.refine_queries: list[str] = []

    async def run_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        query: str,
    ) -> SearchExecution:
        del telegram_user_id, username
        self.search_queries.append(query)
        return SearchExecution(criteria=build_criteria(), apartments=[build_apartment()])

    async def refine_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        message: str,
    ) -> SearchExecution:
        del telegram_user_id, username
        self.refine_queries.append(message)
        return SearchExecution(criteria=build_refined_criteria(), apartments=[build_apartment()])

    async def get_active_criteria(
        self,
        *,
        telegram_user_id: int,
    ) -> SearchCriteria | None:
        del telegram_user_id
        return self.active_criteria

    async def get_saved_apartments(
        self,
        *,
        telegram_user_id: int,
        limit: int = 10,
    ) -> list[EnrichedApartment]:
        del telegram_user_id, limit
        return [build_apartment()]

    async def get_monitor_status(
        self,
        *,
        telegram_user_id: int,
    ) -> MonitorStatus | None:
        del telegram_user_id
        return MonitorStatus(enabled=True, interval_minutes=360)

    def get_default_monitor_status(self) -> MonitorStatus:
        return MonitorStatus(enabled=False, interval_minutes=360)


def build_apartment() -> EnrichedApartment:
    return EnrichedApartment(
        apartment=Apartment(
            external_id="300400",
            source="krisha",
            url="https://krisha.kz/a/show/300400",
            title="Dialog apartment",
            price_kzt=28_000_000,
            city="Almaty",
            rooms=2,
            area_m2=54.0,
            floor="8/10",
            photos=["https://photos.krisha.kz/300400/1.jpg"],
            published_at=datetime(2025, 3, 1, tzinfo=UTC),
        )
    )


def build_criteria() -> SearchCriteria:
    return SearchCriteria(
        user_id=77,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        max_price_kzt=45_000_000,
        rooms=[2, 3],
        page_limit=3,
    )


def build_refined_criteria() -> SearchCriteria:
    return SearchCriteria(
        user_id=77,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        max_price_kzt=35_000_000,
        rooms=[3],
        page_limit=3,
    )


def test_dialog_intent_node_classifies_help_and_search() -> None:
    node = DialogIntentNode()

    assert node.classify(message="Помощь", has_active_criteria=False) == "help"
    assert (
        node.classify(
            message="Ищу 2-комнатную квартиру в Алматы до 45 млн",
            has_active_criteria=False,
        )
        == "search"
    )


def test_dialog_intent_node_classifies_refinement_with_active_criteria() -> None:
    node = DialogIntentNode()

    assert (
        node.classify(
            message="только 3 комнаты и до 35 млн",
            has_active_criteria=True,
        )
        == "refine"
    )


@pytest.mark.asyncio
async def test_dialog_agent_runs_search_for_free_text_query() -> None:
    service = DummyDialogService(active_criteria=None)
    agent = DialogAgent(service)  # type: ignore[arg-type]

    result = await agent.handle_message(
        telegram_user_id=77,
        username="tester",
        message="Ищу 2-комнатную квартиру в Алматы до 45 млн",
    )

    assert result.search_execution is not None
    assert result.search_execution.criteria.city == "Almaty"
    assert result.next_state == "waiting_for_feedback"
    assert service.search_queries == ["Ищу 2-комнатную квартиру в Алматы до 45 млн"]


@pytest.mark.asyncio
async def test_dialog_agent_refines_existing_criteria() -> None:
    service = DummyDialogService(active_criteria=build_criteria())
    agent = DialogAgent(service)  # type: ignore[arg-type]

    result = await agent.handle_message(
        telegram_user_id=77,
        username="tester",
        message="только 3 комнаты и до 35 млн",
    )

    assert result.search_execution is not None
    assert result.search_execution.criteria.max_price_kzt == 35_000_000
    assert result.next_state == "waiting_for_feedback"
    assert service.refine_queries == ["только 3 комнаты и до 35 млн"]


@pytest.mark.asyncio
async def test_dialog_agent_handles_saved_requests() -> None:
    service = DummyDialogService(active_criteria=build_criteria())
    agent = DialogAgent(service)  # type: ignore[arg-type]

    saved_result = await agent.handle_message(
        telegram_user_id=77,
        username="tester",
        message="покажи сохраненные квартиры",
    )

    assert saved_result.search_execution is None
    assert "Сохраненные квартиры" in saved_result.messages[0]


@pytest.mark.asyncio
async def test_dialog_agent_handles_monitor_requests() -> None:
    service = DummyDialogService(active_criteria=build_criteria())
    agent = DialogAgent(service)  # type: ignore[arg-type]

    monitor_result = await agent.handle_message(
        telegram_user_id=77,
        username="tester",
        message="какой сейчас мониторинг",
    )

    assert monitor_result.search_execution is None
    assert "Статус мониторинга" in monitor_result.messages[0]
