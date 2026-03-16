"""Supervisor-style dialog agent for Telegram free-text interactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypedDict

from bot.formatters import (
    format_criteria,
    format_monitor_status,
    format_saved_apartments,
    format_start_message,
)
from bot.service import ActiveCriteriaNotFoundError, SearchBotService, SearchExecution

DialogIntent = Literal[
    "search",
    "refine",
    "show_saved",
    "show_criteria",
    "show_monitor",
    "help",
]
DialogNextState = Literal["clear", "waiting_for_feedback"]

_HELP_MARKERS = ("помощ", "help", "что умеешь", "команды", "что дальше")
_SAVED_MARKERS = ("сохран", "избран", "list", "список квартир")
_CRITERIA_MARKERS = ("критер", "criteria", "параметр")
_MONITOR_MARKERS = ("монитор", "monitor", "интервал")
_REFINE_MARKERS = (
    "уточ",
    "измен",
    "добав",
    "\u0443\u0431\u0435\u0440",
    "остав",
    "только",
    "поменя",
    "обнов",
)
_SEARCH_MARKERS = (
    "ищу",
    "нужн",
    "найд",
    "подбер",
    "покажи",
    "квартир",
    "квартира",
    "аренд",
    "сним",
    "куп",
    "продаж",
)
_FIELD_MARKERS = (
    "до ",
    "от ",
    "район",
    "комнат",
    "площад",
    "pages",
    "страниц",
    "алмат",
    "астан",
    "шымкент",
)


@dataclass(slots=True, frozen=True)
class DialogTurnResult:
    """Dialog agent output returned to Telegram router."""

    messages: list[str] = field(default_factory=list)
    search_execution: SearchExecution | None = None
    next_state: DialogNextState = "clear"


class DialogTurnState(TypedDict, total=False):
    """State carried through one dialog turn."""

    telegram_user_id: int
    username: str | None
    message: str
    has_active_criteria: bool
    intent: DialogIntent
    result: DialogTurnResult


class DialogIntentNode:
    """Rule-based intent classifier for Telegram dialog turns."""

    async def __call__(self, state: DialogTurnState) -> DialogTurnState:
        intent = self.classify(
            message=state["message"],
            has_active_criteria=state["has_active_criteria"],
        )
        return {
            **state,
            "intent": intent,
        }

    def classify(self, *, message: str, has_active_criteria: bool) -> DialogIntent:
        """Classify free-text dialog turn into one of the supported actions."""
        normalized = message.strip().lower()

        if any(marker in normalized for marker in _HELP_MARKERS):
            return "help"
        if any(marker in normalized for marker in _MONITOR_MARKERS):
            return "show_monitor"
        if any(marker in normalized for marker in _SAVED_MARKERS):
            return "show_saved"
        if any(marker in normalized for marker in _CRITERIA_MARKERS):
            return "show_criteria"
        if has_active_criteria and self._looks_like_refinement(normalized):
            return "refine"
        if self._looks_like_search_query(normalized):
            return "search"
        if has_active_criteria:
            return "refine"
        return "help"

    @staticmethod
    def _looks_like_refinement(text: str) -> bool:
        return any(marker in text for marker in _REFINE_MARKERS) or (
            not any(marker in text for marker in _SEARCH_MARKERS)
            and any(marker in text for marker in _FIELD_MARKERS)
        )

    @staticmethod
    def _looks_like_search_query(text: str) -> bool:
        return any(marker in text for marker in _SEARCH_MARKERS) or any(
            marker in text for marker in _FIELD_MARKERS
        )


class DialogAgent:
    """Supervisor that routes one Telegram turn to the matching async tool."""

    def __init__(
        self,
        service: SearchBotService,
        *,
        intent_node: DialogIntentNode | None = None,
    ) -> None:
        self._service = service
        self._classify_intent = (intent_node or DialogIntentNode()).classify

    async def handle_message(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        message: str,
    ) -> DialogTurnResult:
        """Handle one free-text user message through the dialog router."""
        active_criteria = await self._service.get_active_criteria(
            telegram_user_id=telegram_user_id,
        )
        state: DialogTurnState = {
            "telegram_user_id": telegram_user_id,
            "username": username,
            "message": message,
            "has_active_criteria": active_criteria is not None,
            "intent": self._classify_intent(
                message=message,
                has_active_criteria=active_criteria is not None,
            ),
        }
        intent = state["intent"]
        if intent == "search":
            return (await self._handle_search(state))["result"]
        if intent == "refine":
            return (await self._handle_refine(state))["result"]
        if intent == "show_saved":
            return (await self._handle_show_saved(state))["result"]
        if intent == "show_criteria":
            return (await self._handle_show_criteria(state))["result"]
        if intent == "show_monitor":
            return (await self._handle_show_monitor(state))["result"]
        return (await self._handle_help(state))["result"]

    async def _handle_search(self, state: DialogTurnState) -> DialogTurnState:
        execution = await self._service.run_search(
            telegram_user_id=state["telegram_user_id"],
            username=state.get("username"),
            query=state["message"],
        )
        return {
            **state,
            "result": DialogTurnResult(
                search_execution=execution,
                next_state="waiting_for_feedback",
            ),
        }

    async def _handle_refine(self, state: DialogTurnState) -> DialogTurnState:
        try:
            execution = await self._service.refine_search(
                telegram_user_id=state["telegram_user_id"],
                username=state.get("username"),
                message=state["message"],
            )
        except ActiveCriteriaNotFoundError:
            return {
                **state,
                "result": DialogTurnResult(
                    messages=[
                        "Активные критерии не найдены. Сначала выполни поиск через /search."
                    ]
                ),
            }

        return {
            **state,
            "result": DialogTurnResult(
                search_execution=execution,
                next_state="waiting_for_feedback",
            ),
        }

    async def _handle_show_saved(self, state: DialogTurnState) -> DialogTurnState:
        apartments = await self._service.get_saved_apartments(
            telegram_user_id=state["telegram_user_id"],
        )
        return {
            **state,
            "result": DialogTurnResult(
                messages=[format_saved_apartments(apartments)]
            ),
        }

    async def _handle_show_criteria(self, state: DialogTurnState) -> DialogTurnState:
        criteria = await self._service.get_active_criteria(
            telegram_user_id=state["telegram_user_id"],
        )
        if criteria is None:
            return {
                **state,
                "result": DialogTurnResult(
                    messages=[
                        "Активные критерии не найдены. Сначала выполни поиск через /search."
                    ]
                ),
            }
        return {
            **state,
            "result": DialogTurnResult(
                messages=[format_criteria(criteria)]
            ),
        }

    async def _handle_show_monitor(self, state: DialogTurnState) -> DialogTurnState:
        status = await self._service.get_monitor_status(
            telegram_user_id=state["telegram_user_id"],
        )
        if status is None:
            status = self._service.get_default_monitor_status()
        return {
            **state,
            "result": DialogTurnResult(
                messages=[format_monitor_status(status)]
            ),
        }

    async def _handle_help(self, state: DialogTurnState) -> DialogTurnState:
        return {
            **state,
            "result": DialogTurnResult(
                messages=[
                    format_start_message(),
                    "Можно писать запросы обычным текстом без /search, например: "
                    "2-комнатная квартира в Алматы до 45 млн",
                ]
            ),
        }
