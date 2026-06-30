"""Tests for the 2GIS client geocode cache."""

from __future__ import annotations

import httpx
import pytest

from agent.tools.two_gis_client import NearbySummary, TwoGISClient


class FakeCache:
    """In-memory async cache implementing NearbyCacheProtocol."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, name: str) -> str | None:
        return self.store.get(name)

    async def set(self, name: str, value: str, *, ex: int) -> None:
        del ex
        self.store[name] = value


def make_transport(calls: dict[str, int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocode" in str(request.url):
            calls["geocode"] = calls.get("geocode", 0) + 1
            return httpx.Response(
                200,
                json={"result": {"items": [{"point": {"lat": 43.24, "lon": 76.95}}]}},
            )
        return httpx.Response(200, json={"result": {"total": 5}})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_geocode_is_cached_across_calls() -> None:
    calls: dict[str, int] = {}
    cache = FakeCache()
    client = TwoGISClient(api_key="k", cache=cache, transport=make_transport(calls))

    first = await client.get_nearby_summary(city="Almaty", address="Абая 10")
    second = await client.get_nearby_summary(city="Almaty", address="Абая 10")

    assert first == NearbySummary(schools=5, parks=5, metro=5)
    assert second == first
    assert calls["geocode"] == 1  # second lookup served from cache
    assert cache.store  # point was stored


@pytest.mark.asyncio
async def test_place_counts_are_cached_across_calls() -> None:
    counts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocode" in str(request.url):
            return httpx.Response(
                200,
                json={"result": {"items": [{"point": {"lat": 43.24, "lon": 76.95}}]}},
            )
        counts["n"] += 1
        return httpx.Response(200, json={"result": {"total": 7}})

    cache = FakeCache()
    client = TwoGISClient(api_key="k", cache=cache, transport=httpx.MockTransport(handler))

    await client.get_nearby_summary(city="Almaty", address="Абая 10")
    await client.get_nearby_summary(city="Almaty", address="Абая 10")

    # 3 categories counted once on the first call; the second call is fully cached.
    assert counts["n"] == 3


@pytest.mark.asyncio
async def test_geocode_miss_is_cached_to_avoid_refetch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"items": []}})

    calls = {"n": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return handler(request)

    cache = FakeCache()
    client = TwoGISClient(
        api_key="k", cache=cache, transport=httpx.MockTransport(counting_handler)
    )

    assert await client.get_nearby_summary(city="Almaty", address="nowhere") is None
    assert await client.get_nearby_summary(city="Almaty", address="nowhere") is None
    assert calls["n"] == 1  # miss cached, no second geocode request
