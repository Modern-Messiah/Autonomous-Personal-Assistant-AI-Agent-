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


def test_budget_reports_user_window_first() -> None:
    clock = FakeClock()
    budget = SearchBudget(
        per_user_limit=1, global_limit=100, clock=clock
    )

    assert budget.try_acquire(7) is BudgetDecision.ALLOWED
    assert budget.try_acquire(7) is BudgetDecision.USER_EXHAUSTED
    # another user is unaffected by the per-user window...
    assert budget.try_acquire(8) is BudgetDecision.ALLOWED


def test_budget_global_window_caps_all_users_together() -> None:
    clock = FakeClock()
    budget = SearchBudget(
        per_user_limit=10, global_limit=2, clock=clock
    )

    assert budget.try_acquire(1) is BudgetDecision.ALLOWED
    assert budget.try_acquire(2) is BudgetDecision.ALLOWED
    # per-user windows are fresh, but the deployment is out of budget
    assert budget.try_acquire(3) is BudgetDecision.GLOBAL_EXHAUSTED
    assert budget.try_acquire(4) is BudgetDecision.GLOBAL_EXHAUSTED
    # the failed attempts must not have recorded phantom per-user usage
    clock.advance(3601.0)
    assert budget.try_acquire(3) is BudgetDecision.ALLOWED


@pytest.mark.asyncio
async def test_service_blocks_paid_search_when_budget_spent() -> None:
    from bot.service import SearchBotService

    async def fake_runner(*args: object, **kwargs: object) -> list[object]:
        return []

    # the runner returns no apartments, so the session factory is never touched
    service = SearchBotService(
        session_factory=None,  # type: ignore[arg-type]
        search_runner=fake_runner,
        search_budget=SearchBudget(
            per_user_limit=1, global_limit=10, clock=FakeClock()
        ),
    )

    first = await service._run_search_graph(
        telegram_user_id=7, user_id=1, criteria=_build_criteria()
    )
    assert first == []

    with pytest.raises(SearchExecutionError) as exc_info:
        await service._run_search_graph(
            telegram_user_id=7, user_id=1, criteria=_build_criteria()
        )
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
        search_budget=SearchBudget(
            per_user_limit=10, global_limit=1, clock=clock
        ),
    )
    budget = service._search_budget
    assert budget is not None
    budget.try_acquire(999)  # a different user drains the global window

    with pytest.raises(SearchExecutionError) as exc_info:
        await service._run_search_graph(
            telegram_user_id=7, user_id=1, criteria=_build_criteria()
        )
    assert exc_info.value.user_message == SEARCH_BUSY_MESSAGE
