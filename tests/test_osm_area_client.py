"""Tests for the free OSM (Nominatim + Overpass) area client."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from agent.tools.osm_area_client import OsmAreaClient, _classify, _overpass_query


class FakeCache:
    """Minimal async key/value cache matching NearbyCacheProtocol."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str]] = []

    async def get(self, name: str) -> str | None:
        return self.store.get(name)

    async def set(self, name: str, value: str, *, ex: int) -> None:
        self.store[name] = value
        self.set_calls.append((name, value))


def _nom(*, lat: str, lon: str, name: str) -> dict[str, object]:
    return {"lat": lat, "lon": lon, "display_name": name}


def _nominatim_response(lat: float = 43.2400, lon: float = 76.9100) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=[_nom(lat=str(lat), lon=str(lon), name="Алматы, Казахстан")],
    )


def _overpass_response() -> httpx.Response:
    # 2 schools (one as a way with a center), 1 park, and metro counted as one
    # station despite two entrances sharing its name.
    return httpx.Response(
        status_code=200,
        json={
            "elements": [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 43.2405,
                    "lon": 76.9105,
                    "tags": {"amenity": "school", "name": "Школа 1"},
                },
                {
                    "type": "way",
                    "id": 2,
                    "center": {"lat": 43.2410, "lon": 76.9110},
                    "tags": {"amenity": "school", "name": "Школа 2"},
                },
                {
                    "type": "node",
                    "id": 3,
                    "lat": 43.2395,
                    "lon": 76.9095,
                    "tags": {"leisure": "park", "name": "Парк"},
                },
                {
                    "type": "node",
                    "id": 4,
                    "lat": 43.2402,
                    "lon": 76.9102,
                    "tags": {"railway": "subway_entrance", "name": "Станция 1"},
                },
                {
                    "type": "node",
                    "id": 5,
                    "lat": 43.2408,
                    "lon": 76.9108,
                    "tags": {"railway": "subway_entrance", "name": "Станция 1"},
                },
            ]
        },
    )


def _handler_factory(
    *,
    city_center: tuple[float, float] = (43.2389, 76.8894),
    exact: list[dict[str, object]] | None = None,
    street_only: list[dict[str, object]] | None = None,
    overpass: httpx.Response | None = None,
) -> httpx.MockTransport:
    """Nominatim/Overpass mock routing on the `q` parameter.

    - q == the bare city name  -> the city anchor;
    - q containing a digit     -> the exact-address candidates;
    - anything else            -> the street-level candidates (fallback).
    """
    if exact is None:
        exact = [_nom(lat="43.2366", lon="76.9222", name="30, улица Сатпаева, Алматы")]
    if street_only is None:
        street_only = [_nom(lat="43.2382", lon="76.8864", name="проспект Абая, Алматы")]

    def handler(request: httpx.Request) -> httpx.Response:
        if "nominatim" in str(request.url):
            q = dict(request.url.params).get("q", "")
            if "," not in q:  # bare city lookup
                return httpx.Response(
                    status_code=200,
                    json=[
                        _nom(
                            lat=str(city_center[0]),
                            lon=str(city_center[1]),
                            name="Алматы, Казахстан",
                        )
                    ],
                )
            if any(ch.isdigit() for ch in q):
                return httpx.Response(status_code=200, json=exact)
            return httpx.Response(status_code=200, json=street_only)
        if "overpass" in str(request.url):
            from urllib.parse import unquote_plus

            body = unquote_plus(request.content.decode("utf-8"))
            assert "amenity=school" in body
            assert "leisure=park" in body
            assert "railway=subway_entrance" in body
            return overpass if overpass is not None else _overpass_response()
        raise AssertionError(f"unexpected request to {request.url}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_nearby_summary_counts_and_distances() -> None:
    client = OsmAreaClient(transport=_handler_factory(), min_request_interval_seconds=0.0)

    summary = await client.get_nearby_summary(city="Almaty", address="Сатпаева 30")

    assert summary is not None
    assert summary.schools == 2
    assert summary.parks == 1
    # two entrances of ONE station — counted as one metro
    assert summary.metro == 1
    assert summary.schools_nearest_m is not None and summary.schools_nearest_m > 0
    assert summary.metro_nearest_m is not None
    # every measured distance must sit inside the 2 km search radius
    assert summary.schools_nearest_m < 2000
    assert summary.metro_nearest_m < 2000


@pytest.mark.asyncio
async def test_metro_stays_none_outside_metro_cities() -> None:
    client = OsmAreaClient(transport=_handler_factory(), min_request_interval_seconds=0.0)

    summary = await client.get_nearby_summary(city="Astana", address="Сатпаева 30")

    assert summary is not None
    assert summary.schools == 2
    assert summary.metro is None
    assert summary.metro_nearest_m is None


@pytest.mark.asyncio
async def test_far_exact_hit_falls_back_to_street_level() -> None:
    # the exact address resolves to a far-edge suburb — the street-level retry
    # (closer to the center) must win, because OSM house numbers in KZ are sparse
    exact = [_nom(lat="43.2118", lon="76.7817", name="7, улица Абая, Алматы")]
    client = OsmAreaClient(
        transport=_handler_factory(exact=exact), min_request_interval_seconds=0.0
    )
    cache = FakeCache()
    client_with_cache = OsmAreaClient(
        transport=_handler_factory(exact=exact),
        cache=cache,
        min_request_interval_seconds=0.0,
    )

    point = await client._geocode_address(city="Almaty", address="Абая 7")
    assert point == (43.2382, 76.8864)

    # the final answer is cached once under the full address key
    assert await client_with_cache._geocode(city="Almaty", address="Абая 7") == (
        43.2382,
        76.8864,
    )
    geo_keys = [name for name, _ in cache.set_calls if name.startswith("osm:geo:")]
    assert geo_keys == ["osm:geo:almaty|абая 7"]


@pytest.mark.asyncio
async def test_same_named_street_in_another_city_is_rejected() -> None:
    # "Момышулы 12" nominatim top-hits live in ASTANA (51.14, 71.48) — ~1200 km
    # away; the filter must drop them and keep the in-city candidate.
    exact = [
        _nom(lat="51.1394", lon="71.4788", name="ЖК, проспект Момышулы, район Алматы, Астана"),
        _nom(lat="43.2261", lon="76.8596", name="12, улица Момышулы, Алматы"),
    ]
    client = OsmAreaClient(
        transport=_handler_factory(exact=exact), min_request_interval_seconds=0.0
    )

    point = await client._geocode_address(city="Almaty", address="Момышулы 12")

    assert point == (43.2261, 76.8596)


@pytest.mark.asyncio
async def test_geocode_miss_returns_none_and_caches_the_miss() -> None:
    cache = FakeCache()
    client = OsmAreaClient(
        transport=_handler_factory(exact=[], street_only=[]),
        cache=cache,
        min_request_interval_seconds=0.0,
    )

    assert await client.get_nearby_summary(city="Almaty", address="Ноунейм 99") is None
    # the miss is cached so a repeated listing address never re-hits Nominatim
    assert any(value == "" for _, value in cache.set_calls)
    assert await client.get_nearby_summary(city="Almaty", address="Ноунейм 99") is None
    geocode_keys = [name for name, _ in cache.set_calls if name.startswith("osm:geo:")]
    assert len(geocode_keys) == 1
    # the city anchor is cached separately and stays a hit, not a miss
    city_keys = [name for name, value in cache.set_calls if name.startswith("osm:city:")]
    assert len(city_keys) == 1 and cache.store[city_keys[0]].count("|") == 2


@pytest.mark.asyncio
async def test_poi_counts_are_cached_per_point() -> None:
    cache = FakeCache()
    client = OsmAreaClient(
        transport=_handler_factory(), cache=cache, min_request_interval_seconds=0.0
    )

    first = await client.get_nearby_summary(city="Almaty", address="Сатпаева 30")
    second = await client.get_nearby_summary(city="Almaty", address="Сатпаева 30")

    assert first is not None and second is not None
    assert first == second
    count_keys = [name for name, _ in cache.set_calls if name.startswith("osm:cnt:")]
    assert len(count_keys) == 1


@pytest.mark.asyncio
async def test_overpass_failure_degrades_to_no_summary() -> None:
    client = OsmAreaClient(
        transport=_handler_factory(overpass=httpx.Response(status_code=500, text="busy")),
        min_request_interval_seconds=0.0,
    )

    assert await client.get_nearby_summary(city="Almaty", address="Сатпаева 30") is None


def test_overpass_query_pins_the_radius_and_point() -> None:
    query = _overpass_query(43.24, 76.91, 2000)
    assert "(around:2000,43.240000,76.910000)" in query
    # ways/relations carry a center; every category must request it
    assert query.count("out center;") == 1


def test_classify_reports_true_zero_with_no_nearest() -> None:
    counted = _classify([], lat=43.0, lon=76.0, with_metro=True)
    assert counted["schools"] == (0, None)
    assert counted["parks"] == (0, None)
    assert counted["metro"] == (0, None)


def test_settings_default_to_free_osm_provider() -> None:
    from config.settings import APISettings

    api = APISettings(deepseek_api_key=SecretStr("k"))
    assert api.area_provider == "osm"
    # the 2GIS key is optional in OSM mode and required in 2GIS mode
    assert api.two_gis_api_key is None

    with pytest.raises(ValueError, match="two_gis_api_key"):
        APISettings(deepseek_api_key=SecretStr("k"), area_provider="2gis")

    paid = APISettings(
        deepseek_api_key=SecretStr("k"),
        area_provider="2gis",
        two_gis_api_key=SecretStr("key"),
    )
    assert paid.area_provider == "2gis"


def test_blank_two_gis_key_is_treated_as_unset() -> None:
    from config.settings import APISettings

    api = APISettings(deepseek_api_key=SecretStr("k"), two_gis_api_key="  ")

    assert api.two_gis_api_key is None


def _mirror_transport(primary_status: int, *, mirror_status: int = 200) -> httpx.MockTransport:
    """Primary overpass answers with a failure; the mirror serves the data."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "nominatim" in str(request.url):
            q = dict(request.url.params).get("q", "")
            if "," not in q:
                return _nominatim_response(lat="43.2389", lon="76.8894")
            return httpx.Response(
                status_code=200,
                json=[_nom(lat="43.2366", lon="76.9222", name="30, улица Сатпаева, Алматы")],
            )
        if "overpass-api.de" in str(request.url):
            return httpx.Response(status_code=primary_status, text="busy")
        if "maps.mail.ru" in str(request.url):
            if mirror_status != 200:
                return httpx.Response(status_code=mirror_status, text="busy")
            return _overpass_response()
        raise AssertionError(f"unexpected request to {request.url}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_overpass_mirror_takes_over_when_primary_is_down() -> None:
    client = OsmAreaClient(
        transport=_mirror_transport(primary_status=503),
        min_request_interval_seconds=0.0,
    )

    summary = await client.get_nearby_summary(city="Almaty", address="Сатпаева 30")

    assert summary is not None
    assert summary.schools == 2


@pytest.mark.asyncio
async def test_all_overpass_instances_down_degrades_to_no_summary() -> None:
    client = OsmAreaClient(
        transport=_mirror_transport(primary_status=500, mirror_status=500),
        min_request_interval_seconds=0.0,
    )

    assert await client.get_nearby_summary(city="Almaty", address="Сатпаева 30") is None
