"""Tests for the cross-process krisha fetch lock."""

from __future__ import annotations

import asyncio

import pytest

from agent.tools.fetch_lock import FETCH_LOCK_KEY, RedisFetchLock


class InMemoryRedis:
    """SET NX / GET / DELETE semantics of the real client, in memory."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int | None, bool]] = []

    async def set(
        self, name: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        self.set_calls.append((name, value, ex, nx))
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    async def get(self, name: str) -> str | None:
        return self.store.get(name)

    async def delete(self, name: str) -> int:
        return 1 if self.store.pop(name, None) is not None else 0


@pytest.mark.asyncio
async def test_hold_claims_with_ttl_and_releases() -> None:
    redis = InMemoryRedis()
    lock = RedisFetchLock(redis)

    async with lock.hold():
        assert FETCH_LOCK_KEY in redis.store
        # the claim carries a TTL so a crashed holder frees itself
        name, _, ex, nx = redis.set_calls[0]
        assert name == FETCH_LOCK_KEY
        assert ex is not None and ex > 0
        assert nx is True

    assert FETCH_LOCK_KEY not in redis.store


@pytest.mark.asyncio
async def test_second_holder_queues_until_first_releases() -> None:
    redis = InMemoryRedis()
    first = RedisFetchLock(redis, poll_interval_seconds=0.01)
    second = RedisFetchLock(redis, poll_interval_seconds=0.01)
    order: list[str] = []
    first_may_finish = asyncio.Event()

    async def hold_first() -> None:
        async with first.hold():
            order.append("first-in")
            await first_may_finish.wait()
        order.append("first-out")

    async def hold_second() -> None:
        # let the first holder claim the lock before we try
        await asyncio.sleep(0.02)
        async with second.hold():
            order.append("second-in")

    async def release_first() -> None:
        await asyncio.sleep(0.05)
        first_may_finish.set()

    await asyncio.gather(hold_first(), hold_second(), release_first())

    # the second fetch strictly waited for the first to finish
    assert order == ["first-in", "first-out", "second-in"]
    assert FETCH_LOCK_KEY not in redis.store


@pytest.mark.asyncio
async def test_wait_timeout_proceeds_without_stealing_the_lock() -> None:
    redis = InMemoryRedis()
    redis.store[FETCH_LOCK_KEY] = "someone-elses-token"
    lock = RedisFetchLock(redis, poll_interval_seconds=0.01, max_wait_seconds=0.05)

    async with lock.hold():
        pass  # proceeded after the bounded wait — serving beats failing

    # we never owned the key, so releasing must not evict the real holder
    assert redis.store[FETCH_LOCK_KEY] == "someone-elses-token"


@pytest.mark.asyncio
async def test_release_skips_foreign_token_after_ttl_expiry() -> None:
    redis = InMemoryRedis()
    lock = RedisFetchLock(redis, poll_interval_seconds=0.01)

    async with lock.hold():
        # simulate our TTL expiring mid-fetch and another process claiming it
        redis.store[FETCH_LOCK_KEY] = "new-holder-token"

    assert redis.store[FETCH_LOCK_KEY] == "new-holder-token"


@pytest.mark.asyncio
async def test_search_node_scrapes_inside_the_lock() -> None:
    from contextlib import asynccontextmanager

    from agent.models.criteria import SearchCriteria
    from agent.nodes.search_node import SearchNode

    redis = InMemoryRedis()
    lock = RedisFetchLock(redis, poll_interval_seconds=0.01)
    held_during_search: list[bool] = []

    class LockAwareParser:
        async def search(self, context: object, criteria: SearchCriteria) -> list:
            held_during_search.append(FETCH_LOCK_KEY in redis.store)
            return []

    @asynccontextmanager
    async def factory():
        yield object()

    node = SearchNode(parser=LockAwareParser(), context_factory=factory, fetch_lock=lock)
    criteria = SearchCriteria(
        user_id=1, city="Almaty", deal_type="sale", property_type="apartment"
    )

    result = await node({"criteria": criteria, "apartments": []})

    # the krisha phase ran with the lock held, and it was freed right after
    assert held_during_search == [True]
    assert FETCH_LOCK_KEY not in redis.store
    assert result["apartments"] == []
