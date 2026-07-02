"""Minimal 2GIS client used by enrichment node."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Protocol

import httpx

from agent.tools.http_retry import request_with_retry

logger = logging.getLogger(__name__)

# Cities (canonical/RU, lowercased) that actually have a metro system. Elsewhere
# the "metro station" lookup returns nothing, so it is skipped to save quota.
_METRO_CITIES = {"almaty", "алматы"}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in meters."""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


@dataclass(slots=True, frozen=True)
class NearbySummary:
    """Nearby infrastructure around a listing: counts + distance to the nearest.

    ``*_nearest_m`` is the straight-line distance (meters) to the closest object of
    that category, or None when unknown/none within the search radius.
    """

    schools: int | None
    parks: int | None
    metro: int | None
    schools_nearest_m: int | None = None
    parks_nearest_m: int | None = None
    metro_nearest_m: int | None = None


class NearbyCacheProtocol(Protocol):
    """Minimal async key/value cache for geocode results and place counts."""

    async def get(self, name: str) -> str | None: ...

    async def set(self, name: str, value: str, *, ex: int) -> None: ...


class TwoGISClient:
    """HTTP client for fetching nearby places from 2GIS APIs."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 10.0,
        radius_meters: int = 2000,
        cache: NearbyCacheProtocol | None = None,
        geocode_ttl_seconds: int = 2_592_000,
        geocode_miss_ttl_seconds: int = 86_400,
        counts_ttl_seconds: int = 604_800,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._radius_meters = radius_meters
        self._cache = cache
        self._geocode_ttl_seconds = geocode_ttl_seconds
        self._geocode_miss_ttl_seconds = geocode_miss_ttl_seconds
        self._counts_ttl_seconds = counts_ttl_seconds
        self._transport = transport
        self._geocode_url = "https://catalog.api.2gis.com/3.0/items/geocode"
        self._items_url = "https://catalog.api.2gis.com/3.0/items"

    async def get_nearby_summary(self, *, city: str, address: str) -> NearbySummary | None:
        """Resolve listing point and return nearby counts for key categories."""
        point = await self._geocode(city=city, address=address)
        if point is None:
            return None
        lat, lon = point

        schools, schools_m = await self._count_nearby(query="school", lat=lat, lon=lon)
        parks, parks_m = await self._count_nearby(query="park", lat=lat, lon=lon)
        # Only Almaty has a metro in Kazakhstan; querying "metro station" anywhere
        # else just wastes a 2GIS call and returns no data (which then looked like
        # a misleading "метро: 0"). Skip it for cities without a metro system.
        metro, metro_m = (None, None)
        if city.strip().lower() in _METRO_CITIES:
            metro, metro_m = await self._count_nearby(query="metro station", lat=lat, lon=lon)
        return NearbySummary(
            schools=schools,
            parks=parks,
            metro=metro,
            schools_nearest_m=schools_m,
            parks_nearest_m=parks_m,
            metro_nearest_m=metro_m,
        )

    async def _geocode(self, *, city: str, address: str) -> tuple[float, float] | None:
        cache_key = f"2gis:geo:{city.strip().lower()}|{address.strip().lower()}"
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                # "" is a cached miss; anything else is "lat,lon".
                return self._decode_cached_point(cached)

        point = await self._geocode_api(city=city, address=address)

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

    async def _geocode_api(self, *, city: str, address: str) -> tuple[float, float] | None:
        # 2GIS omits geometry unless explicitly requested, so without
        # fields=items.point every geocode result lacks lat/lon and enrichment
        # silently degrades to zero nearby counts.
        params = {
            "q": f"{city}, {address}",
            "fields": "items.point",
            "key": self._api_key,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await request_with_retry(
                    lambda: client.get(self._geocode_url, params=params)
                )
        except httpx.HTTPError:
            return None

        data = response.json()
        items = data.get("result", {}).get("items", [])
        if not items:
            return None

        point = items[0].get("point", {})
        lat = point.get("lat")
        lon = point.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return None
        return float(lat), float(lon)

    @staticmethod
    def _decode_cached_point(cached: str) -> tuple[float, float] | None:
        if not cached:
            return None
        try:
            lat_str, lon_str = cached.split(",", 1)
            return float(lat_str), float(lon_str)
        except ValueError:
            return None

    async def _count_nearby(
        self, *, query: str, lat: float, lon: float
    ) -> tuple[int | None, int | None]:
        """Return (count, distance-to-nearest-in-meters) for a category near a point."""
        # Counts + nearest distance around a point change slowly, so cache them per
        # query+coordinate (rounded ~11m) to cut repeat 2GIS requests. Cached as
        # "count,dist" where either side may be empty for "unknown".
        cache_key = f"2gis:cnt:v3:{query}:{lat:.4f}:{lon:.4f}:{self._radius_meters}"
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                decoded = self._decode_cached_count(cached)
                if decoded is not None:
                    return decoded

        count, nearest_m = await self._count_nearby_api(query=query, lat=lat, lon=lon)

        if self._cache is not None and count is not None:
            payload = f"{count},{nearest_m if nearest_m is not None else ''}"
            await self._cache.set(cache_key, payload, ex=self._counts_ttl_seconds)
        return count, nearest_m

    @staticmethod
    def _decode_cached_count(cached: str) -> tuple[int | None, int | None] | None:
        count_str, _, dist_str = cached.partition(",")
        try:
            count = int(count_str)
        except ValueError:
            return None
        nearest_m = int(dist_str) if dist_str else None
        return count, nearest_m

    async def _count_nearby_api(
        self, *, query: str, lat: float, lon: float
    ) -> tuple[int | None, int | None]:
        # Ask for the closest items (sorted by distance) and their coordinates so
        # the SAME request yields both the total count and the nearest object's
        # distance — no extra 2GIS quota beyond what the count already costs.
        params: dict[str, str | int] = {
            "q": query,
            "point": f"{lon},{lat}",
            "radius": self._radius_meters,
            "page_size": 10,
            "sort": "distance",
            "type": "branch",
            "fields": "items.point",
            "key": self._api_key,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await request_with_retry(
                    lambda: client.get(self._items_url, params=params)
                )
        except httpx.HTTPError:
            logger.warning("2GIS nearby count request failed query=%s", query)
            return None, None

        try:
            data = response.json()
        except ValueError:
            logger.warning("2GIS nearby count returned invalid JSON query=%s", query)
            return None, None
        result = data.get("result", {})
        total = result.get("total")
        count = total if type(total) is int and total >= 0 else None
        nearest_m = self._nearest_distance_m(result.get("items", []), lat=lat, lon=lon)
        if count is None:
            # On zero matches 2GIS replies HTTP 200 with meta.code=404
            # ("Results not found") and no result block — that is a TRUE zero
            # ("checked, none within the radius"), not missing data.
            meta = data.get("meta")
            if isinstance(meta, dict) and meta.get("code") == 404:
                return 0, None
            if nearest_m is None:
                logger.warning("2GIS nearby count missing valid total query=%s", query)
        return count, nearest_m

    @staticmethod
    def _nearest_distance_m(
        items: object, *, lat: float, lon: float
    ) -> int | None:
        if not isinstance(items, list):
            return None
        distances: list[float] = []
        for item in items:
            point = item.get("point") if isinstance(item, dict) else None
            if not isinstance(point, dict):
                continue
            plat, plon = point.get("lat"), point.get("lon")
            if isinstance(plat, int | float) and isinstance(plon, int | float):
                distances.append(_haversine_m(lat, lon, float(plat), float(plon)))
        return round(min(distances)) if distances else None
