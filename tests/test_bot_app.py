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

    async def refine_search(self, *, telegram_user_id: int, username: str | None, message: str):
        del telegram_user_id, username, message
        raise AssertionError("Handler body should not run in router smoke test")

    async def get_active_criteria(self, *, telegram_user_id: int):
        del telegram_user_id
        return None

    async def get_saved_apartments(self, *, telegram_user_id: int, limit: int = 10):
        del telegram_user_id, limit
        return []

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


def test_create_dispatcher_includes_bot_routes() -> None:
    dispatcher = create_dispatcher(service=DummyService())  # type: ignore[arg-type]

    assert isinstance(dispatcher, Dispatcher)
    update_types = dispatcher.resolve_used_update_types()
    assert "message" in update_types
    assert "callback_query" in update_types
