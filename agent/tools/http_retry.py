"""Small async retry helper: exponential backoff + jitter for transient HTTP errors.

Used by the 2GIS and Notion clients so a single dropped connection or a transient
5xx/429 doesn't immediately degrade enrichment or skip a Notion sync. Non-transient
4xx responses fail fast (retrying won't help).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx

_SERVER_ERROR_FLOOR = 500
_TOO_MANY_REQUESTS = 429


async def request_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 4.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> httpx.Response:
    """Send a request and ``raise_for_status``, retrying transient failures.

    Retries on transport errors and 5xx/429 responses with exponential backoff +
    jitter. Non-429 4xx responses raise immediately. The last error is re-raised
    once ``attempts`` is exhausted.
    """
    last_error: httpx.HTTPError | None = None
    for attempt in range(attempts):
        try:
            response = await send()
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status < _SERVER_ERROR_FLOOR and status != _TOO_MANY_REQUESTS:
                raise  # non-transient client error — retrying won't help
            last_error = exc
        except httpx.TransportError as exc:
            last_error = exc
        if attempt < attempts - 1:
            delay = min(max_delay, base_delay * (2**attempt)) + random.uniform(0, base_delay)
            await sleep(delay)
    assert last_error is not None  # loop ran at least once
    raise last_error
