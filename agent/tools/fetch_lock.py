"""Cross-process mutex serializing krisha.kz fetches.

Every scraping entrypoint (interactive /search in the bot, monitor jobs in the
ARQ worker, the parser canary) runs in its own process with its own browser, so
per-request delays inside one search do nothing when several searches run at
once — krisha sees a burst from one IP, which is how bans and captchas happen.
This lock lives in Redis (already shared by all three processes) and makes the
whole deployment scrape as one polite client: one fetch session at a time,
everyone else queues.

Semantics:
- `SET NX EX` with a per-holder token; release deletes the key only when the
  token still matches, so an expired holder can't free its successor's lock.
- The TTL is the deadlock guard: a crashed/hung holder frees itself in
  ``ttl_seconds`` at worst.
- Waiting is bounded. On ``max_wait_seconds`` the caller proceeds WITHOUT the
  lock (logged loudly): serving the user with a small ban risk beats failing,
  and the pathological case (a stuck holder) already means something is wrong.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

logger = logging.getLogger(__name__)

FETCH_LOCK_KEY = "krisha:fetch:lock"
# A search holds the lock for its whole krisha phase: 1-3 listing pages plus up
# to 6 detail fetches with 1-3s delays ≈ 15-40s. The TTL only has to outlive
# that; 120s leaves slack for slow pages without parking the queue for long.
DEFAULT_TTL_SECONDS = 120
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_WAIT_SECONDS = 180.0


class RedisLockProtocol(Protocol):
    """Redis subset used by the fetch lock."""

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None: ...

    async def get(self, name: str) -> str | None: ...

    async def delete(self, name: str) -> int: ...


class RedisFetchLock:
    """Polling Redis mutex with token-checked release and bounded wait."""

    def __init__(
        self,
        redis_client: RedisLockProtocol,
        *,
        key: str = FETCH_LOCK_KEY,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    ) -> None:
        self._redis = redis_client
        self._key = key
        self._ttl_seconds = ttl_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._max_wait_seconds = max_wait_seconds

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """Run the body as the sole krisha fetcher (best effort after timeout)."""
        token = uuid.uuid4().hex
        acquired = await self._acquire(token)
        try:
            yield
        finally:
            if acquired:
                await self._release(token)

    async def _acquire(self, token: str) -> bool:
        deadline = time.monotonic() + self._max_wait_seconds
        started_waiting: float | None = None
        while True:
            claimed = await self._redis.set(self._key, token, ex=self._ttl_seconds, nx=True)
            if claimed:
                if started_waiting is not None:
                    logger.info(
                        "acquired krisha fetch lock after %.1fs in queue",
                        time.monotonic() - started_waiting,
                    )
                return True
            if started_waiting is None:
                started_waiting = time.monotonic()
                logger.info("krisha fetch lock busy; queueing this search")
            if time.monotonic() >= deadline:
                logger.warning(
                    "krisha fetch lock still busy after %.0fs; proceeding without it",
                    self._max_wait_seconds,
                )
                return False
            # Jitter so several queued waiters don't stampede the same instant.
            await asyncio.sleep(self._poll_interval_seconds + random.uniform(0, 0.25))

    async def _release(self, token: str) -> None:
        # Only free our own claim: if the TTL expired mid-fetch and someone else
        # took the lock, deleting blindly would evict them. (GET+DEL has a tiny
        # race window vs a Lua script — acceptable at this scale.)
        holder = await self._redis.get(self._key)
        if holder == token:
            await self._redis.delete(self._key)
