"""Tests for the shared async HTTP retry helper."""

from __future__ import annotations

import httpx
import pytest

from agent.tools.http_retry import request_with_retry


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", "http://example.test"))


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_returns_immediately_on_success() -> None:
    calls = {"n": 0}

    async def send() -> httpx.Response:
        calls["n"] += 1
        return _response(200)

    result = await request_with_retry(send, sleep=_no_sleep)
    assert result.status_code == 200
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retries_transient_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    async def send() -> httpx.Response:
        calls["n"] += 1
        return _response(503) if calls["n"] < 3 else _response(200)

    result = await request_with_retry(send, attempts=3, sleep=_no_sleep)
    assert result.status_code == 200
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_raises_after_attempts_exhausted() -> None:
    calls = {"n": 0}

    async def send() -> httpx.Response:
        calls["n"] += 1
        return _response(500)

    with pytest.raises(httpx.HTTPStatusError):
        await request_with_retry(send, attempts=3, sleep=_no_sleep)
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_client_4xx_not_retried() -> None:
    calls = {"n": 0}

    async def send() -> httpx.Response:
        calls["n"] += 1
        return _response(404)

    with pytest.raises(httpx.HTTPStatusError):
        await request_with_retry(send, attempts=3, sleep=_no_sleep)
    assert calls["n"] == 1  # non-transient client error is not retried


@pytest.mark.asyncio
async def test_429_is_retried() -> None:
    calls = {"n": 0}

    async def send() -> httpx.Response:
        calls["n"] += 1
        return _response(429) if calls["n"] < 2 else _response(200)

    result = await request_with_retry(send, attempts=3, sleep=_no_sleep)
    assert result.status_code == 200
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_transport_error_retried_then_reraised() -> None:
    calls = {"n": 0}

    async def send() -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    with pytest.raises(httpx.ConnectError):
        await request_with_retry(send, attempts=2, sleep=_no_sleep)
    assert calls["n"] == 2
