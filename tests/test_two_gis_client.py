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


@pytest.mark.asyncio
async def test_failed_place_counts_are_unknown_and_not_cached() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocode" in str(request.url):
            return httpx.Response(
                200,
                json={"result": {"items": [{"point": {"lat": 43.24, "lon": 76.95}}]}},
            )
        return httpx.Response(503)

    cache = FakeCache()
    client = TwoGISClient(api_key="k", cache=cache, transport=httpx.MockTransport(handler))

    summary = await client.get_nearby_summary(city="Almaty", address="Абая 10")

    assert summary == NearbySummary(schools=None, parks=None, metro=None)
    assert not any(key.startswith("2gis:cnt:") for key in cache.store)


@pytest.mark.asyncio
async def test_valid_zero_is_cached_in_versioned_namespace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocode" in str(request.url):
            return httpx.Response(
                200,
                json={"result": {"items": [{"point": {"lat": 43.24, "lon": 76.95}}]}},
            )
        return httpx.Response(200, json={"result": {"total": 0}})

    cache = FakeCache()
    client = TwoGISClient(api_key="k", cache=cache, transport=httpx.MockTransport(handler))

    summary = await client.get_nearby_summary(city="Almaty", address="Абая 10")

    assert summary == NearbySummary(schools=0, parks=0, metro=0)
    assert len([key for key in cache.store if key.startswith("2gis:cnt:v3:")]) == 3


@pytest.mark.asyncio
async def test_missing_total_is_unknown_instead_of_page_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocode" in str(request.url):
            return httpx.Response(
                200,
                json={"result": {"items": [{"point": {"lat": 43.24, "lon": 76.95}}]}},
            )
        return httpx.Response(200, json={"result": {"items": [{"id": "one"}]}})

    client = TwoGISClient(api_key="k", transport=httpx.MockTransport(handler))

    assert await client.get_nearby_summary(
        city="Almaty", address="Абая 10"
    ) == NearbySummary(schools=None, parks=None, metro=None)


@pytest.mark.asyncio
async def test_zero_matches_meta_404_is_true_zero() -> None:
    # On zero matches 2GIS replies HTTP 200 + meta.code=404 "Results not found":
    # that is a real 0 ("checked, none within radius"), not unknown — e.g. metro
    # in Almaty's Alatau district, far from the metro line.
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocode" in str(request.url):
            return httpx.Response(
                200,
                json={"result": {"items": [{"point": {"lat": 43.27, "lon": 76.81}}]}},
            )
        return httpx.Response(
            200,
            json={
                "meta": {
                    "code": 404,
                    "error": {"message": "Results not found", "type": "itemNotFound"},
                }
            },
        )

    client = TwoGISClient(api_key="k", transport=httpx.MockTransport(handler))
    summary = await client.get_nearby_summary(city="Almaty", address="Момышулы 12")

    assert summary == NearbySummary(schools=0, parks=0, metro=0)


@pytest.mark.asyncio
async def test_metro_query_skipped_for_city_without_metro() -> None:
    from urllib.parse import parse_qs, urlparse

    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "geocode" in url:
            return httpx.Response(
                200,
                json={"result": {"items": [{"point": {"lat": 43.24, "lon": 76.95}}]}},
            )
        queries.append(parse_qs(urlparse(url).query).get("q", [""])[0])
        return httpx.Response(200, json={"result": {"total": 3}})

    client = TwoGISClient(api_key="k", transport=httpx.MockTransport(handler))
    summary = await client.get_nearby_summary(city="Taraz", address="Толе би 40")

    assert summary.schools == 3
    assert summary.parks == 3
    assert summary.metro is None and summary.metro_nearest_m is None
    assert "metro station" not in queries  # not even requested (saves quota)


@pytest.mark.asyncio
async def test_nearby_summary_reports_distance_to_nearest() -> None:
    from agent.tools.two_gis_client import _haversine_m

    listing_lat, listing_lon = 43.240, 76.950
    near_lat, near_lon = 43.244, 76.950  # closest of the returned items

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocode" in str(request.url):
            return httpx.Response(
                200,
                json={"result": {"items": [{"point": {"lat": listing_lat, "lon": listing_lon}}]}},
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "total": 4,
                    "items": [
                        {"point": {"lat": near_lat, "lon": near_lon}},
                        {"point": {"lat": 43.300, "lon": 76.990}},  # farther away
                    ],
                }
            },
        )

    client = TwoGISClient(api_key="k", transport=httpx.MockTransport(handler))
    summary = await client.get_nearby_summary(city="Almaty", address="Абая 10")

    expected_m = round(_haversine_m(listing_lat, listing_lon, near_lat, near_lon))
    assert summary.metro == 4
    assert summary.metro_nearest_m == expected_m
    assert summary.schools_nearest_m == expected_m  # nearest of the two items
