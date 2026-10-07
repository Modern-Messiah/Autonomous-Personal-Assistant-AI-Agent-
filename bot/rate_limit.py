"""Per-user and deployment-wide budgets for paid searches (LLM/2GIS/krisha).

The message throttle caps *actions* per minute, but one search costs orders of
magnitude more than one chat message: a single run burns DeepSeek quota, a
handful of 2GIS calls and a Playwright crawl of krisha. On an open bot that is
the real budget to bound — a stranger looping /search at the message throttle
could run the paid pipeline near-continuously.

Two layers with different jobs:
- per-user window — fair share for each individual;
- global window — caps total spend for the whole deployment. The bot is open,
  so N strangers x the per-user limit is still an unbounded bill and a crawl
  of krisha from one IP; the global window is the actual wallet guard.

Two interchangeable backends:
- in-memory (default): exact sliding windows, valid while the bot runs as the
  single process it is today — every user-triggered search funnels through
  ``SearchBotService._run_search_graph`` in that one process;
- Redis (``SearchBudget(redis=...)``): sorted-set windows shared across
  processes/replicas, atomic via a small Lua script (deny decisions record
  nothing, so failed attempts never leave phantom usage). Wall-clock scores
  because processes no longer share a monotonic clock.

Both backends fail OPEN when Redis errors mid-flight: these windows guard
paid quota, they are not a security boundary, and brick-walling every search
because Redis hiccuped is the worse outage.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_WINDOW_SECONDS = 3600.0

# Key for the single shared global window (Telegram user ids are positive).
GLOBAL_BUDGET_KEY = 0


class RedisEvalProtocol(Protocol):
    """Minimal redis client contract: atomic script evaluation."""

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> Any: ...


# Atomic two-window budget check. KEYS[1] = per-user zset, KEYS[2] = global zset.
# ARGV: now_ms, window_ms, user_limit, global_limit, unique_member.
# Returns 0 allowed (both windows recorded), 1 user-exhausted, 2 global-exhausted;
# a denied attempt records nothing, so it cannot leave phantom usage.
_BUDGET_LUA = """
local function sweep(key, now, window)
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
end
local now, window = tonumber(ARGV[1]), tonumber(ARGV[2])
sweep(KEYS[1], now, window)
sweep(KEYS[2], now, window)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 1 end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[4]) then return 2 end
redis.call('ZADD', KEYS[1], now, ARGV[5])
redis.call('ZADD', KEYS[2], now, ARGV[5])
redis.call('PEXPIRE', KEYS[1], window * 2)
redis.call('PEXPIRE', KEYS[2], window * 2)
return 0
"""

_REDIS_DECISIONS = {0: "allowed", 1: "user_exhausted", 2: "global_exhausted"}


class BudgetDecision(Enum):
    """Outcome of one paid-search budget check."""

    ALLOWED = "allowed"
    USER_EXHAUSTED = "user_exhausted"
    GLOBAL_EXHAUSTED = "global_exhausted"

    @classmethod
    def _from_redis(cls, value: Any) -> BudgetDecision:
        try:
            return cls(_REDIS_DECISIONS[int(value)])
        except (KeyError, TypeError, ValueError):
            logger.warning("search-budget lua returned unexpected value %r", value)
            return cls.ALLOWED


class SearchRateLimiter:
    """In-memory sliding-window budget of searches per key per hour."""

    def __init__(
        self,
        *,
        limit_per_hour: int,
        window_seconds: float = DEFAULT_SEARCH_WINDOW_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if limit_per_hour < 1:
            msg = "limit_per_hour must be at least 1"
            raise ValueError(msg)
        self._limit = limit_per_hour
        self._window = window_seconds
        self._starts: dict[int, deque[float]] = defaultdict(deque)
        # monotonic by default so wall-clock jumps (NTP) cannot free budget.
        self._clock = clock or time.monotonic

    def can_acquire(self, key: int) -> bool:
        """Whether a slot is free, WITHOUT recording one."""
        now = self._clock()
        starts = self._starts[key]
        cutoff = now - self._window
        count = sum(1 for start in starts if start > cutoff)
        return count < self._limit

    def try_acquire(self, key: int) -> bool:
        """Record one search start; False when the hourly budget is spent."""
        now = self._clock()
        starts = self._starts[key]
        cutoff = now - self._window
        while starts and starts[0] <= cutoff:
            starts.popleft()
        if len(starts) >= self._limit:
            return False
        starts.append(now)
        return True

    def retry_after_seconds(self, key: int) -> float:
        """Seconds until the oldest counted search leaves the window (0 when free)."""
        starts = self._starts.get(key)
        if not starts:
            return 0.0
        wait = self._window - (self._clock() - starts[0])
        return max(wait, 0.0)


class SearchBudget:
    """Per-user + global hourly budget for paid searches.

    In-memory mode decides both windows without an await between peek and
    record, so the sequence stays atomic within the event loop. Redis mode
    decides and records both windows in one atomic Lua call.
    """

    def __init__(
        self,
        *,
        per_user_limit: int,
        global_limit: int,
        window_seconds: float = DEFAULT_SEARCH_WINDOW_SECONDS,
        clock: Callable[[], float] | None = None,
        redis: RedisEvalProtocol | None = None,
        redis_prefix: str = "krisha:budget",
    ) -> None:
        if per_user_limit < 1 or global_limit < 1:
            msg = "budget limits must be at least 1"
            raise ValueError(msg)
        self._per_user = SearchRateLimiter(
            limit_per_hour=per_user_limit, window_seconds=window_seconds, clock=clock
        )
        self._global = SearchRateLimiter(
            limit_per_hour=global_limit, window_seconds=window_seconds, clock=clock
        )
        self._per_user_limit = per_user_limit
        self._global_limit = global_limit
        self._window_seconds = window_seconds
        self._redis = redis
        self._redis_prefix = redis_prefix

    async def try_acquire(self, telegram_user_id: int) -> BudgetDecision:
        """Record one search against both windows and report the binding one."""
        if self._redis is not None:
            return await self._try_acquire_redis(telegram_user_id)
        return self._try_acquire_memory(telegram_user_id)

    def _try_acquire_memory(self, telegram_user_id: int) -> BudgetDecision:
        if not self._per_user.can_acquire(telegram_user_id):
            return BudgetDecision.USER_EXHAUSTED
        if not self._global.try_acquire(GLOBAL_BUDGET_KEY):
            return BudgetDecision.GLOBAL_EXHAUSTED
        self._per_user.try_acquire(telegram_user_id)
        return BudgetDecision.ALLOWED

    async def _try_acquire_redis(self, telegram_user_id: int) -> BudgetDecision:
        now_ms = int(time.time() * 1000)
        window_ms = int(self._window_seconds * 1000)
        try:
            raw = await self._redis.eval(  # type: ignore[union-attr]
                _BUDGET_LUA,
                2,
                f"{self._redis_prefix}:user:{telegram_user_id}",
                f"{self._redis_prefix}:global",
                now_ms,
                window_ms,
                self._per_user_limit,
                self._global_limit,
                uuid.uuid4().hex,
            )
        except Exception:
            # Fail open: a quota guard, not a security boundary — a Redis
            # outage must not brick every search.
            logger.warning("search budget redis error; allowing search", exc_info=True)
            return BudgetDecision.ALLOWED
        return BudgetDecision._from_redis(raw)

    async def retry_after_seconds(self, telegram_user_id: int) -> float:
        """Seconds until the binding window frees a slot (best effort, memory only)."""
        if self._redis is not None:
            return 0.0
        user_wait = self._per_user.retry_after_seconds(telegram_user_id)
        global_wait = self._global.retry_after_seconds(GLOBAL_BUDGET_KEY)
        return max(user_wait, global_wait, 0.0)
