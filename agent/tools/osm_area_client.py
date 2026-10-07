"""Free OpenStreetMap area client: Nominatim geocoding + Overpass POI counts.

Drop-in replacement for :class:`agent.tools.two_gis_client.TwoGISClient` with
the exact same contract (``get_nearby_summary`` → ``NearbySummary``): resolve a
listing's address to a point, then count schools/parks/metro within the radius
and report the nearest distance per category. No API key and no per-call cost —
the public instances are rate-limited instead, so this client:

- self-throttles geocoding to the Nominatim public-instance policy (max 1 rps);
- leans on the same Redis caches the 2GIS client used (geocode hits ~30 days,
  misses ~1 day, POI counts ~7 days), keeping real request volume tiny;
- sends one Overpass query per point for ALL categories at once (2GIS needed
  three).

Data quality note: OSM coverage of Kazakhstani schools/parks/metro is good in
the big cities but is community-maintained, so counts can differ from 2GIS
by a listing or two. Scoring treats the counts comparatively within a batch,
so this does not change recommendation quality.

Metro: only Almaty has a system (kept in sync with the 2GIS client) — the
metro field stays None elsewhere so cards show the same "нет данных" as now.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from agent.tools.http_retry import request_with_retry
from agent.tools.two_gis_client import (
    NearbyCacheProtocol,
    NearbySummary,
    _haversine_m,
)

logger = logging.getLogger(__name__)

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_INTERPRETER_URL = "https://overpass-api.de/api/interpreter"

# Nominatim public instance policy: absolute max 1 request/second.
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 1.1

# Only Almaty has a metro in Kazakhstan — same set the 2GIS client skips.
_METRO_CITIES = {"almaty", "алматы"}

# Polite identification required by the Nominatim usage policy.
_USER_AGENT = (
    "krisha-agent/0.1 (personal Telegram apartment-search bot; "
    "github.com/Modern-Messiah/Autonomous-Personal-Assistant-AI-Agent-)"
)

_RADIUS = 2000

# A candidate farther than this from the geocoded city center is a same-named
# street in ANOTHER settlement (Astana literally has an "Алматы" district).
_MAX_CITY_RADIUS_M = 30_000
# An exact-address hit this close to the center wins outright; farther ones
# trigger the street-level retry (OSM house-number coverage in KZ is sparse).
_NEAR_CITY_RADIUS_M = 8_000


@dataclass(slots=True, frozen=True)
class _CityAnchor:
    """City center plus the display-name parts that identify it."""

    point: tuple[float, float]
    names: set[str]


@dataclass(slots=True, frozen=True)
class _Candidate:
    """One geocode candidate ranked for plausibility."""

    point: tuple[float, float]
    distance_m: float
    mentions_city: bool

    @property
    def sort_key(self) -> tuple[bool, float]:
        # city-named hits first, then the closest to the center
        return (not self.mentions_city, self.distance_m)


def _display_parts(display_name: object) -> set[str]:
    """Comma-separated lowercase parts of a Nominatim display_name."""
    if not isinstance(display_name, str):
        return set()
    return {part.strip().lower() for part in display_name.split(",") if part.strip()}


def _overpass_query(lat: float, lon: float, radius_meters: int) -> str:
    """One query for every category: schools, parks and metro entrances."""
    around = f"(around:{radius_meters},{lat:.6f},{lon:.6f})"
    return (
        "[out:json][timeout:25];"
        "("
        f"node{around}[amenity=school];"
        f"way{around}[amenity=school];"
        f"node{around}[leisure=park];"
        f"way{around}[leisure=park];"
        f"relation{around}[leisure=park];"
        f"node{around}[railway=subway];"
        f"node{around}[railway=subway_entrance];"
        f'node{around}[railway=station][station=subway];'
        ");"
        "out center;"
    )


def _element_point(element: dict[str, Any]) -> tuple[float, float] | None:
    """Coordinates of an Overpass element: nodes carry lat/lon, ways a center."""
    lat, lon = element.get("lat"), element.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    center = element.get("center")
    if isinstance(center, dict):
        clat, clon = center.get("lat"), center.get("lon")
        if isinstance(clat, (int, float)) and isinstance(clon, (int, float)):
            return float(clat), float(clon)
    return None


def _classify(
    elements: list[dict[str, Any]],
    *,
    lat: float,
    lon: float,
    with_metro: bool,
) -> dict[str, tuple[int | None, int | None]]:
    """Per-category (count, nearest-distance) from raw Overpass elements."""
    school_distances: list[float] = []
    park_distances: list[float] = []
    metro_distances: list[float] = []
    metro_names: set[str | None] = set()

    for element in elements:
        tags = element.get("tags")
        if not isinstance(tags, dict):
            continue
        point = _element_point(element)
        if point is None:
            continue
        distance = _haversine_m(lat, lon, point[0], point[1])
        if tags.get("amenity") == "school":
            school_distances.append(distance)
        elif tags.get("leisure") == "park":
            park_distances.append(distance)
        elif with_metro and (
            tags.get("railway") in {"subway", "subway_entrance"}
            or (tags.get("railway") == "station" and tags.get("station") == "subway")
        ):
            metro_distances.append(distance)
            # entrances share the station name — count stations, not doors
            name = tags.get("name")
            metro_names.add(name if isinstance(name, str) else None)

    def summary(distances: list[float], count: int | None) -> tuple[int | None, int | None]:
        if count is None:
            return None, None
        return count, round(min(distances)) if distances else None

    schools = summary(school_distances, len(school_distances))
    parks = summary(park_distances, len(park_distances))
    metro = summary(metro_distances, len(metro_names)) if with_metro else (None, None)
    return {"schools": schools, "parks": parks, "metro": metro}


class OsmAreaClient:
    """OSM-backed implementation of the enrichment area client."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        radius_meters: int = _RADIUS,
        cache: NearbyCacheProtocol | None = None,
        geocode_ttl_seconds: int = 2_592_000,
        geocode_miss_ttl_seconds: int = 86_400,
        counts_ttl_seconds: int = 604_800,
        min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        nominatim_url: str = NOMINATIM_SEARCH_URL,
        overpass_url: str = OVERPASS_INTERPRETER_URL,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._radius_meters = radius_meters
        self._cache = cache
        self._geocode_ttl_seconds = geocode_ttl_seconds
        self._geocode_miss_ttl_seconds = geocode_miss_ttl_seconds
        self._counts_ttl_seconds = counts_ttl_seconds
        self._transport = transport
        self._nominatim_url = nominatim_url
        self._overpass_url = overpass_url
        # Serializes requests and spaces them out for the public instances.
        self._gate = asyncio.Lock()
        self._min_interval_seconds = min_request_interval_seconds
        self._last_request_monotonic = 0.0

    async def get_nearby_summary(self, *, city: str, address: str) -> NearbySummary | None:
        """Resolve listing point and return nearby counts for key categories."""
        point = await self._geocode(city=city, address=address)
        if point is None:
            return None
        lat, lon = point
        elements = await self._fetch_pois(lat=lat, lon=lon)
        if elements is None:
            return None
        with_metro = city.strip().lower() in _METRO_CITIES
        counted = _classify(elements, lat=lat, lon=lon, with_metro=with_metro)
        return NearbySummary(
            schools=counted["schools"][0],
            parks=counted["parks"][0],
            metro=counted["metro"][0],
            schools_nearest_m=counted["schools"][1],
            parks_nearest_m=counted["parks"][1],
            metro_nearest_m=counted["metro"][1],
        )

    async def _paced(self) -> None:
        """Space public-instance requests out (Nominatim policy: max 1 rps)."""
        async with self._gate:
            now = time.monotonic()
            wait = self._last_request_monotonic + self._min_interval_seconds - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_monotonic = time.monotonic()

    async def _geocode(self, *, city: str, address: str) -> tuple[float, float] | None:
        cache_key = f"osm:geo:{city.strip().lower()}|{address.strip().lower()}"
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                # "" is a cached miss; anything else is "lat,lon".
                return self._decode_cached_point(cached)

        point = await self._geocode_address(city=city, address=address)

        if self._cache is not None:
            if point is None:
                await self._cache.set(cache_key, "", ex=self._geocode_miss_ttl_seconds)
            else:
                await self._cache.set(
                    cache_key,
                    f"{point[0]},{point[1]}",
                    ex=self._geocode_ttl_seconds,
                )
        return point

    async def _geocode_address(
        self, *, city: str, address: str
    ) -> tuple[float, float] | None:
        """Address → point, with Kazakhstan-specific guarding.

        Nominatim freely returns same-named streets from OTHER settlements
        (an "Алматы" district exists in Astana, a "улица Абая" exists in half
        the region's villages), so candidates are filtered against the city
        center: prefer hits that mention the city itself, drop everything too
        far from it. If the exact address misses or lands on the city's far
        edge, retry with the house number stripped — OSM street coverage is
        much better than its house-number coverage in KZ.
        """
        center = await self._city_center(city)
        if center is None:
            return None

        primary = await self._best_candidate(
            f"{city}, {address}", center=center.point, city_names=center.names
        )
        if primary is not None and primary.distance_m <= _NEAR_CITY_RADIUS_M:
            return primary.point

        street_only = re.sub(r"\s*\d+[^,]*", "", address).strip(" ,")
        secondary = None
        if street_only and street_only != address.strip():
            secondary = await self._best_candidate(
                f"{city}, {street_only}", center=center.point, city_names=center.names
            )
        if secondary is not None and (
            primary is None or secondary.distance_m < primary.distance_m
        ):
            return secondary.point
        return primary.point if primary is not None else None

    async def _city_center(self, city: str) -> _CityAnchor | None:
        """City center point + its own display-name parts (cached long-term)."""
        cache_key = f"osm:city:{city.strip().lower()}"
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                try:
                    lat_str, lon_str, names_blob = cached.split("|", 2)
                    return _CityAnchor(
                        (float(lat_str), float(lon_str)),
                        set(names_blob.split(";")) if names_blob else set(),
                    )
                except ValueError:
                    pass  # corrupted entry — re-resolve

        results = await self._nominatim_search(q=city, limit=1)
        if not results:
            return None
        first = results[0]
        try:
            point = float(first["lat"]), float(first["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        names = _display_parts(first.get("display_name"))
        if self._cache is not None:
            await self._cache.set(
                cache_key,
                f"{point[0]}|{point[1]}|{';'.join(sorted(names))}",
                ex=self._geocode_ttl_seconds,
            )
        return _CityAnchor(point, names)

    async def _best_candidate(
        self, query: str, *, center: tuple[float, float], city_names: set[str]
    ) -> _Candidate | None:
        """Closest sane hit: city-named first, then by distance to the center."""
        results = await self._nominatim_search(q=query, limit=10)
        best: _Candidate | None = None
        for result in results:
            try:
                point = float(result["lat"]), float(result["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            distance_m = _haversine_m(center[0], center[1], point[0], point[1])
            if distance_m > _MAX_CITY_RADIUS_M:
                continue  # another settlement's same-named street
            mentions_city = bool(city_names & _display_parts(result.get("display_name")))
            candidate = _Candidate(point, distance_m, mentions_city)
            if best is None or candidate.sort_key < best.sort_key:
                best = candidate
        return best

    async def _nominatim_search(
        self, *, q: str, limit: int
    ) -> list[dict[str, Any]]:
        params = {
            "q": q,
            "format": "jsonv2",
            "limit": str(limit),
            "accept-language": "ru",
            "countrycodes": "kz",
        }
        headers = {"User-Agent": _USER_AGENT}
        try:
            await self._paced()
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await request_with_retry(
                    lambda: client.get(self._nominatim_url, params=params, headers=headers)
                )
        except httpx.HTTPError:
            logger.warning("OSM geocode request failed q=%r", q)
            return []

        try:
            results = response.json()
        except ValueError:
            return []
        return results if isinstance(results, list) else []

    @staticmethod
    def _decode_cached_point(cached: str) -> tuple[float, float] | None:
        if not cached:
            return None
        try:
            lat_str, lon_str = cached.split(",", 1)
            return float(lat_str), float(lon_str)
        except ValueError:
            return None

    async def _fetch_pois(self, *, lat: float, lon: float) -> list[dict[str, Any]] | None:
        cache_key = f"osm:cnt:v1:{lat:.4f}:{lon:.4f}:{self._radius_meters}"
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                try:
                    decoded = json.loads(cached)
                except ValueError:
                    decoded = None
                if isinstance(decoded, list):
                    return decoded

        elements = await self._fetch_pois_api(lat=lat, lon=lon)

        if self._cache is not None and elements is not None:
            await self._cache.set(
                cache_key, json.dumps(elements), ex=self._counts_ttl_seconds
            )
        return elements

    async def _fetch_pois_api(
        self, *, lat: float, lon: float
    ) -> list[dict[str, Any]] | None:
        query = _overpass_query(lat, lon, self._radius_meters)
        try:
            await self._paced()
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds + 15.0,
                transport=self._transport,
            ) as client:
                response = await request_with_retry(
                    lambda: client.post(
                        self._overpass_url,
                        data={"data": query},
                        headers={"User-Agent": _USER_AGENT},
                    )
                )
        except httpx.HTTPError:
            logger.warning("Overpass POI request failed")
            return None

        try:
            payload = response.json()
        except ValueError:
            return None
        elements = payload.get("elements") if isinstance(payload, dict) else None
        if not isinstance(elements, list):
            return None
        return elements
