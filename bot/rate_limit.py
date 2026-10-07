"""Per-user budget for paid searches (LLM scoring + 2GIS + krisha scraping).

The message throttle caps *actions* per minute, but one search costs orders of
magnitude more than one chat message: a single run burns DeepSeek quota, a
handful of 2GIS calls and a Playwright crawl of krisha. On an open bot that is
the real budget to bound — a stranger looping /search at the message throttle
could run the paid pipeline near-continuously.

Like :class:`bot.middlewares.ThrottleMiddleware` this limiter lives in the bot
process only: searches triggered from the Telegram update handler all funnel
through ``SearchBotService._run_search_graph`` in that one process, so an
in-memory sliding window is sufficient (monitor jobs in the scheduler are
already serialized by their own intervals and the Redis fetch lock).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

DEFAULT_SEARCH_WINDOW_SECONDS = 3600.0


class SearchRateLimiter:
    """Sliding-window budget of searches per Telegram user per hour."""

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

    def try_acquire(self, telegram_user_id: int) -> bool:
        """Record one search start; False when the user's hourly budget is spent."""
        now = self._clock()
        starts = self._starts[telegram_user_id]
        cutoff = now - self._window
        while starts and starts[0] <= cutoff:
            starts.popleft()
        if len(starts) >= self._limit:
            return False
        starts.append(now)
        return True

    def retry_after_seconds(self, telegram_user_id: int) -> float:
        """Seconds until the oldest counted search leaves the window (0 when free)."""
        starts = self._starts.get(telegram_user_id)
        if not starts:
            return 0.0
        wait = self._window - (self._clock() - starts[0])
        return max(wait, 0.0)
