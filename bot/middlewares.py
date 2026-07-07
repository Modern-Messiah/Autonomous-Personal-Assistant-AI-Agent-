"""Access-control and rate-limit middlewares for the Telegram bot.

Two outer middlewares guard every update:
- AllowlistMiddleware drops updates from users not on the allowlist (when one is
  configured), so a public bot doesn't burn 2GIS/DeepSeek quota on strangers.
- ThrottleMiddleware caps how many actions a single user can trigger per minute.

Both run in the bot process only (the scheduler doesn't poll), so an in-memory
sliding window is enough — no shared store needed.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, User

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

_ACCESS_DENIED = "🚫 Бот приватный. Доступ выдаёт владелец."
_TOO_FAST = "⏳ Слишком много запросов. Подождите немного и повторите."


def _user_of(event: TelegramObject) -> User | None:
    return getattr(event, "from_user", None)


async def _reply(event: TelegramObject, text: str) -> None:
    answer = getattr(event, "answer", None)
    if answer is None:
        return
    # CallbackQuery.answer wants show_alert; Message.answer doesn't take it.
    if isinstance(event, CallbackQuery):
        await answer(text, show_alert=True)
    else:
        await answer(text)


class AllowlistMiddleware(BaseMiddleware):
    """Pass updates only from allowed users; an empty allowlist means open."""

    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        self._allowed = allowed_user_ids

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._allowed:
            return await handler(event, data)
        user = _user_of(event)
        if user is not None and user.id in self._allowed:
            return await handler(event, data)
        if user is not None:
            await _reply(event, _ACCESS_DENIED)
        return None


class ThrottleMiddleware(BaseMiddleware):
    """Sliding-window per-user rate limit; warns once per window when exceeded."""

    def __init__(self, per_minute: int, *, window_seconds: float = 60.0) -> None:
        self._limit = per_minute
        self._window = window_seconds
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._warned: dict[int, float] = {}

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = _user_of(event)
        if user is None:
            return await handler(event, data)
        now = time.monotonic()
        hits = self._hits[user.id]
        cutoff = now - self._window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self._limit:
            # Warn at most once per window so a flood can't be amplified into spam.
            if now - self._warned.get(user.id, 0.0) >= self._window:
                self._warned[user.id] = now
                await _reply(event, _TOO_FAST)
            return None
        hits.append(now)
        return await handler(event, data)
