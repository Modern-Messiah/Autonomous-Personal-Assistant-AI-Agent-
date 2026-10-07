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

Like :class:`bot.middlewares.ThrottleMiddleware` these live in the bot process
only: searches triggered from the Telegram update handler all funnel through
``SearchBotService._run_search_graph`` in that one process, so in-memory sliding
windows are sufficient (monitor jobs in the scheduler are already serialized by
their own intervals and the Redis fetch lock).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from enum import Enum

DEFAULT_SEARCH_WINDOW_SECONDS = 3600.0

# Key for the single shared global window (Telegram user ids are positive).
GLOBAL_BUDGET_KEY = 0


class SearchRateLimiter:
    """Sliding-window budget of searches per key per hour."""

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


class BudgetDecision(Enum):
    """Outcome of one paid-search budget check."""

    ALLOWED = "allowed"
    USER_EXHAUSTED = "user_exhausted"
    GLOBAL_EXHAUSTED = "global_exhausted"


class SearchBudget:
    """Per-user + global hourly budget for paid searches.

    ``try_acquire`` is synchronous (no awaits), so the peek-then-record sequence
    below is atomic within the event loop: the per-user ``try_acquire`` after a
    successful ``can_acquire`` cannot fail.
    """

    def __init__(
        self,
        *,
        per_user_limit: int,
        global_limit: int,
        window_seconds: float = DEFAULT_SEARCH_WINDOW_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._per_user = SearchRateLimiter(
            limit_per_hour=per_user_limit, window_seconds=window_seconds, clock=clock
        )
        self._global = SearchRateLimiter(
            limit_per_hour=global_limit, window_seconds=window_seconds, clock=clock
        )

    def try_acquire(self, telegram_user_id: int) -> BudgetDecision:
        """Record one search against both windows and report the binding one."""
        if not self._per_user.can_acquire(telegram_user_id):
            return BudgetDecision.USER_EXHAUSTED
        if not self._global.try_acquire(GLOBAL_BUDGET_KEY):
            return BudgetDecision.GLOBAL_EXHAUSTED
        self._per_user.try_acquire(telegram_user_id)
        return BudgetDecision.ALLOWED

    def retry_after_seconds(self, telegram_user_id: int) -> float:
        """Seconds until the binding window frees a slot (0 when free)."""
        user_wait = self._per_user.retry_after_seconds(telegram_user_id)
        global_wait = self._global.retry_after_seconds(GLOBAL_BUDGET_KEY)
        return max(user_wait, global_wait, 0.0)
