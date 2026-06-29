"""Router integration tests for Telegram command flows."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Message, Update

from bot.app import create_dispatcher
from bot.service import SEARCH_EXECUTION_ERROR_MESSAGE, SearchExecutionError


class CapturingSession(BaseSession):
    """Session stub that records outgoing bot messages."""

    def __init__(self) -> None:
        super().__init__()
        self.sent_texts: list[str] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: Any,
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        del timeout
        if not isinstance(method, SendMessage):
            msg = f"Unexpected Telegram method in test: {type(method).__name__}"
            raise AssertionError(msg)

        self.sent_texts.append(method.text)
        return Message.model_validate(
            {
                "message_id": len(self.sent_texts),
                "date": 0,
                "chat": {"id": method.chat_id, "type": "private"},
                "text": method.text,
            },
            context={"bot": bot},
        )

    async def stream_content(self, *args: Any, **kwargs: Any):
        del args, kwargs
        if False:
            yield b""


class FailingSearchService:
    """Minimal service stub that fails the command search path."""

    async def register_user(self, *, telegram_user_id: int, username: str | None) -> None:
        del telegram_user_id, username

    async def run_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        query: str,
    ):
        del telegram_user_id, username, query
        raise SearchExecutionError()

    async def refine_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        message: str,
    ):
        del telegram_user_id, username, message
        raise AssertionError("Refine path should not be called in this test")

    async def get_active_criteria(self, *, telegram_user_id: int):
        del telegram_user_id
        return None

    async def get_saved_apartments(self, *, telegram_user_id: int, limit: int = 10):
        del telegram_user_id, limit
        return []

    async def save_apartments(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        apartment_urls: list[str],
    ):
        del telegram_user_id, username, apartment_urls
        return 0

    async def reject_apartments(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        apartment_urls: list[str],
    ):
        del telegram_user_id, username, apartment_urls
        return 0

    async def get_monitor_status(self, *, telegram_user_id: int):
        del telegram_user_id
        return None

    async def set_monitor_enabled(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        enabled: bool,
    ):
        del telegram_user_id, username, enabled
        return None

    async def set_monitor_interval(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        interval_minutes: int,
    ):
        del telegram_user_id, username, interval_minutes
        return None

    def get_default_monitor_status(self):
        return None


def build_command_update(*, text: str) -> Update:
    """Construct a minimal Telegram update for command routing tests."""

    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 0,
                "chat": {"id": 77, "type": "private"},
                "from": {"id": 77, "is_bot": False, "first_name": "Denis"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
            },
        }
    )


@pytest.mark.asyncio
async def test_search_command_replies_with_user_facing_error_when_search_fails() -> None:
    dispatcher = create_dispatcher(service=FailingSearchService())  # type: ignore[arg-type]
    session = CapturingSession()
    bot = Bot(token="123456:ABCDEF", session=session, default=DefaultBotProperties())

    await dispatcher.feed_update(bot, build_command_update(text="/search test"))

    assert session.sent_texts == [
        "Ищу варианты по заданным критериям...",
        SEARCH_EXECUTION_ERROR_MESSAGE,
    ]
