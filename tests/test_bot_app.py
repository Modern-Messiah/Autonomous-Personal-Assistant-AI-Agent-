"""Smoke tests for bot bootstrap."""

from __future__ import annotations

from aiogram import Dispatcher

from bot.app import create_dispatcher


class DummyService:
    """Minimal service stub for router registration."""

    async def register_user(self, *, telegram_user_id: int, username: str | None) -> None:
        del telegram_user_id, username

    async def run_search(self, *, telegram_user_id: int, username: str | None, query: str):
        del telegram_user_id, username, query
        raise AssertionError("Handler body should not run in router smoke test")

    async def get_active_criteria(self, *, telegram_user_id: int):
        del telegram_user_id
        return None


def test_create_dispatcher_includes_bot_routes() -> None:
    dispatcher = create_dispatcher(service=DummyService())  # type: ignore[arg-type]

    assert isinstance(dispatcher, Dispatcher)
    assert dispatcher.resolve_used_update_types()

