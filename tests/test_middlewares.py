"""Tests for the allowlist + throttle guard middlewares."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bot.middlewares import AllowlistMiddleware, ThrottleMiddleware


class FakeMessage:
    """Minimal Message-like object with a from_user and a recording answer()."""

    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


def _passthrough() -> tuple[Any, list[Any]]:
    seen: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        seen.append(event)
        return "handled"

    return handler, seen


@pytest.mark.asyncio
async def test_allowlist_blocks_strangers_and_passes_members() -> None:
    handler, seen = _passthrough()
    mw = AllowlistMiddleware(frozenset({111}))

    member = FakeMessage(111)
    assert await mw(handler, member, {}) == "handled"
    assert seen == [member]
    assert member.answers == []

    stranger = FakeMessage(999)
    assert await mw(handler, stranger, {}) is None
    assert seen == [member]  # handler not called for the stranger
    assert stranger.answers and "приватный" in stranger.answers[0]


@pytest.mark.asyncio
async def test_empty_allowlist_is_open() -> None:
    handler, seen = _passthrough()
    mw = AllowlistMiddleware(frozenset())
    anyone = FakeMessage(42)

    assert await mw(handler, anyone, {}) == "handled"
    assert seen == [anyone]


@pytest.mark.asyncio
async def test_throttle_blocks_after_limit_and_warns_once() -> None:
    handler, seen = _passthrough()
    mw = ThrottleMiddleware(per_minute=3, window_seconds=60.0)
    user_id = 7

    # first 3 pass
    for _ in range(3):
        assert await mw(handler, FakeMessage(user_id), {}) == "handled"
    assert len(seen) == 3

    # 4th and 5th are blocked; only the first blocked one warns
    fourth = FakeMessage(user_id)
    assert await mw(handler, fourth, {}) is None
    assert fourth.answers and "Слишком" in fourth.answers[0]

    fifth = FakeMessage(user_id)
    assert await mw(handler, fifth, {}) is None
    assert fifth.answers == []  # warned once per window
    assert len(seen) == 3  # handler never ran for the blocked ones


@pytest.mark.asyncio
async def test_throttle_is_per_user() -> None:
    handler, seen = _passthrough()
    mw = ThrottleMiddleware(per_minute=1, window_seconds=60.0)

    assert await mw(handler, FakeMessage(1), {}) == "handled"
    assert await mw(handler, FakeMessage(1), {}) is None  # user 1 over budget
    assert await mw(handler, FakeMessage(2), {}) == "handled"  # user 2 unaffected
    assert len(seen) == 2


class FakeThrottleRedis:
    """In-Python mirror of _THROTTLE_LUA semantics (one sorted set per key)."""

    def __init__(self) -> None:
        self.windows: dict[str, dict[str, float]] = {}

    async def eval(self, script: str, numkeys: int, *args: object) -> int:
        assert numkeys == 1
        key, now_ms, window_ms, limit, member = args
        now, window, limit = int(now_ms), int(window_ms), int(limit)
        window_set = self.windows.setdefault(str(key), {})
        for stale in [m for m, score in window_set.items() if score <= now - window]:
            del window_set[stale]
        if len(window_set) >= limit:
            return 0
        window_set[str(member)] = float(now)
        return 1


@pytest.mark.asyncio
async def test_throttle_redis_window_is_shared_per_user() -> None:
    redis = FakeThrottleRedis()
    middleware = ThrottleMiddleware(2, window_seconds=60.0, redis=redis)

    allowed = [await middleware._allow(11) for _ in range(3)]

    assert allowed == [True, True, False]
    # the denied third attempt recorded nothing
    assert len(redis.windows["krisha:throttle:11"]) == 2
    # another user has their own shared window
    assert await middleware._allow(12) is True
    assert "krisha:throttle:12" in redis.windows
