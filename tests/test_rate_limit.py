"""Tests for the per-user and global paid-search budgets."""

from __future__ import annotations

import pytest

from bot.errors import (
    SEARCH_BUSY_MESSAGE,
    SEARCH_RATE_LIMITED_MESSAGE,
    SearchExecutionError,
)
from bot.rate_limit import BudgetDecision, SearchBudget, SearchRateLimiter


class FakeClock:
    """Manual monotonic clock for deterministic window tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_limiter_allows_budget_then_blocks_until_window_slides() -> None:
    clock = FakeClock()
    limiter = SearchRateLimiter(limit_per_hour=2, window_seconds=3600.0, clock=clock)

    assert limiter.try_acquire(42) is True
    assert limiter.try_acquire(42) is True
    # budget spent: further searches are denied
    assert limiter.try_acquire(42) is False
    assert limiter.retry_after_seconds(42) == pytest.approx(3600.0)

    # an hour later the oldest search leaves the window
    clock.advance(3601.0)
    assert limiter.try_acquire(42) is True


def test_limiter_budgets_are_per_user() -> None:
    clock = FakeClock()
    limiter = SearchRateLimiter(limit_per_hour=1, window_seconds=3600.0, clock=clock)

    assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(1) is False
    # another user has their own budget
    assert limiter.try_acquire(2) is True


def test_limiter_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        SearchRateLimiter(limit_per_hour=0)


@pytest.mark.asyncio
async def test_budget_reports_user_window_first() -> None:
    clock = FakeClock()
    budget = SearchBudget(per_user_limit=1, global_limit=100, clock=clock)

    assert await budget.try_acquire(7) is BudgetDecision.ALLOWED
    assert await budget.try_acquire(7) is BudgetDecision.USER_EXHAUSTED
    # another user is unaffected by the per-user window...
    assert await budget.try_acquire(8) is BudgetDecision.ALLOWED


@pytest.mark.asyncio
async def test_budget_global_window_caps_all_users_together() -> None:
    clock = FakeClock()
    budget = SearchBudget(per_user_limit=10, global_limit=2, clock=clock)

    assert await budget.try_acquire(1) is BudgetDecision.ALLOWED
    assert await budget.try_acquire(2) is BudgetDecision.ALLOWED
    # per-user windows are fresh, but the deployment is out of budget
    assert await budget.try_acquire(3) is BudgetDecision.GLOBAL_EXHAUSTED
    assert await budget.try_acquire(4) is BudgetDecision.GLOBAL_EXHAUSTED
    # the failed attempts must not have recorded phantom per-user usage
    clock.advance(3601.0)
    assert await budget.try_acquire(3) is BudgetDecision.ALLOWED


@pytest.mark.asyncio
async def test_service_blocks_paid_search_when_budget_spent() -> None:
    from bot.service import SearchBotService

    async def fake_runner(*args: object, **kwargs: object) -> list[object]:
        return []

    # the runner returns no apartments, so the session factory is never touched
    service = SearchBotService(
        session_factory=None,  # type: ignore[arg-type]
        search_runner=fake_runner,
        search_budget=SearchBudget(per_user_limit=1, global_limit=10, clock=FakeClock()),
    )

    first = await service._run_search_graph(
        telegram_user_id=7, user_id=1, criteria=_build_criteria()
    )
    assert first == []

    with pytest.raises(SearchExecutionError) as exc_info:
        await service._run_search_graph(telegram_user_id=7, user_id=1, criteria=_build_criteria())
    assert exc_info.value.user_message == SEARCH_RATE_LIMITED_MESSAGE


def _build_criteria() -> object:
    from agent.models.criteria import SearchCriteria

    return SearchCriteria(user_id=1, city="Almaty", deal_type="sale")


@pytest.mark.asyncio
async def test_service_reports_busy_when_global_budget_spent() -> None:
    from bot.service import SearchBotService

    async def fake_runner(*args: object, **kwargs: object) -> list[object]:
        return []

    clock = FakeClock()
    service = SearchBotService(
        session_factory=None,  # type: ignore[arg-type]
        search_runner=fake_runner,
        search_budget=SearchBudget(per_user_limit=10, global_limit=1, clock=clock),
    )
    budget = service._search_budget
    assert budget is not None
    await budget.try_acquire(999)  # a different user drains the global window

    with pytest.raises(SearchExecutionError) as exc_info:
        await service._run_search_graph(telegram_user_id=7, user_id=1, criteria=_build_criteria())
    assert exc_info.value.user_message == SEARCH_BUSY_MESSAGE


class FakeBudgetRedis:
    """In-Python mirror of _BUDGET_LUA semantics (sorted sets per key).

    The real script runs inside Redis; this fake keeps unit tests hermetic.
    If the Lua changes, the mirror must change with it — the tests below
    pin the observable contract (deny records nothing).
    """

    def __init__(self) -> None:
        self.windows: dict[str, dict[str, float]] = {}
        self.fail: Exception | None = None

    async def eval(self, script: str, numkeys: int, *args: object) -> int:
        assert numkeys == 2
        if self.fail is not None:
            raise self.fail
        user_key, global_key = args[0], args[1]
        now_ms, window_ms, user_limit, global_limit, member = args[2:]
        now, window, user_limit, global_limit = (
            int(now_ms),
            int(window_ms),
            int(user_limit),
            int(global_limit),
        )

        def count(key: str) -> int:
            window_set = self.windows.get(key)
            if not window_set:
                return 0
            for member_key in [m for m, score in window_set.items() if score <= now - window]:
                del window_set[member_key]
            return len(window_set)

        if count(str(user_key)) >= user_limit:
            return 1
        if count(str(global_key)) >= global_limit:
            return 2
        for key in (str(user_key), str(global_key)):
            self.windows.setdefault(key, {})[str(member)] = float(now)
        return 0


@pytest.mark.asyncio
async def test_redis_budget_caps_user_and_global_without_phantom_usage() -> None:
    redis = FakeBudgetRedis()
    budget = SearchBudget(per_user_limit=2, global_limit=3, window_seconds=3600.0, redis=redis)

    assert await budget.try_acquire(1) is BudgetDecision.ALLOWED
    assert await budget.try_acquire(1) is BudgetDecision.ALLOWED
    # per-user window exhausted; the denied attempt records nothing anywhere
    assert await budget.try_acquire(1) is BudgetDecision.USER_EXHAUSTED
    assert len(redis.windows["krisha:budget:user:1"]) == 2
    assert len(redis.windows["krisha:budget:global"]) == 2

    # a second user still fits the global window once, then caps it
    assert await budget.try_acquire(2) is BudgetDecision.ALLOWED
    assert await budget.try_acquire(3) is BudgetDecision.GLOBAL_EXHAUSTED
    # the global denial left no usage for user 3
    assert "krisha:budget:user:3" not in redis.windows


@pytest.mark.asyncio
async def test_redis_budget_fails_open_on_redis_error() -> None:
    redis = FakeBudgetRedis()
    redis.fail = RuntimeError("connection reset")
    budget = SearchBudget(per_user_limit=1, global_limit=1, window_seconds=3600.0, redis=redis)

    # a quota guard, not a security boundary: Redis down must not brick searches
    assert await budget.try_acquire(1) is BudgetDecision.ALLOWED
    assert await budget.try_acquire(1) is BudgetDecision.ALLOWED
