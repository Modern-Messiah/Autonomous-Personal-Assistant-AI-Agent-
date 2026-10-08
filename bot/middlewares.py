"""Access-control and rate-limit middlewares for the Telegram bot.

Two outer middlewares guard every update:
- AllowlistMiddleware drops updates from users not on the allowlist (when one is
  configured), so a public bot doesn't burn 2GIS/DeepSeek quota on strangers.
- ThrottleMiddleware caps how many actions a single user can trigger per minute.

Both run in the bot process only (the scheduler doesn't poll). The throttle
defaults to an in-memory sliding window — enough for the single bot process the
deployment is today — and can share its window across replicas by passing a
Redis client (atomic Lua window, fail-open on Redis errors: the throttle is a
fairness guard, not a security boundary).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, User

from bot.rate_limit import RedisEvalProtocol

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

_ACCESS_DENIED = "🚫 Бот приватный. Доступ выдаёт владелец."
_TOO_FAST = "⏳ Слишком много запросов. Подождите немного и повторите."

# Atomic sliding window on one sorted-set key. ARGV: now_ms, window_ms, limit.
# Adds a member only when the window is not full, so a denied attempt records
# nothing (no self-amplifying lockout).
_THROTTLE_LUA = """
local now, window = tonumber(ARGV[1]), tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('PEXPIRE', KEYS[1], window * 2)
return 1
"""


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

    def __init__(
        self,
        per_minute: int,
        *,
        window_seconds: float = 60.0,
        redis: RedisEvalProtocol | None = None,
        redis_prefix: str = "krisha:throttle",
    ) -> None:
        self._limit = per_minute
        self._window = window_seconds
        self._redis = redis
        self._redis_prefix = redis_prefix
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._warned: dict[int, float] = {}
        self._seq = 0

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = _user_of(event)
        if user is None:
            return await handler(event, data)
        if not await self._allow(user.id):
            # Warn at most once per window so a flood can't be amplified into spam.
            now = time.monotonic()
            if now - self._warned.get(user.id, 0.0) >= self._window:
                self._warned[user.id] = now
                await _reply(event, _TOO_FAST)
            return None
        return await handler(event, data)

    async def _allow(self, user_id: int) -> bool:
        if self._redis is None:
            return self._allow_memory(user_id)
        return await self._allow_redis(user_id)

    def _allow_memory(self, user_id: int) -> bool:
        now = time.monotonic()
        hits = self._hits[user_id]
        cutoff = now - self._window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True

    async def _allow_redis(self, user_id: int) -> bool:
        now_ms = int(time.time() * 1000)
        window_ms = int(self._window * 1000)
        try:
            raw = await self._redis.eval(  # type: ignore[union-attr]
                _THROTTLE_LUA,
                1,
                f"{self._redis_prefix}:{user_id}",
                now_ms,
                window_ms,
                self._limit,
                uuid.uuid4().hex,
            )
        except Exception:
            # Fail open: a fairness guard, not a security boundary.
            logger.warning("throttle redis error; allowing update", exc_info=True)
            return True
        try:
            return int(raw) == 1
        except (TypeError, ValueError):
            logger.warning("throttle lua returned unexpected value %r", raw)
            return True
