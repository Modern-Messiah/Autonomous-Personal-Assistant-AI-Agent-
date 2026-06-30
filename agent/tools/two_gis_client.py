"""Minimal 2GIS client used by enrichment node."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class NearbySummary:
    """Nearby infrastructure counts around listing location."""

    schools: int | None
    parks: int | None
    metro: int | None


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

        schools = await self._count_nearby(query="school", lat=lat, lon=lon)
        parks = await self._count_nearby(query="park", lat=lat, lon=lon)
        metro = await self._count_nearby(query="metro station", lat=lat, lon=lon)
        return NearbySummary(schools=schools, parks=parks, metro=metro)

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
                response = await client.get(self._geocode_url, params=params)
                response.raise_for_status()
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
    ) -> int | None:
        # Place counts around a point change slowly, so cache them per
        # query+coordinate (rounded ~11m) to cut repeat 2GIS requests.
        cache_key = f"2gis:cnt:v2:{query}:{lat:.4f}:{lon:.4f}:{self._radius_meters}"
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                try:
                    return int(cached)
                except ValueError:
                    pass

        count = await self._count_nearby_api(query=query, lat=lat, lon=lon)

        if self._cache is not None and count is not None:
            await self._cache.set(cache_key, str(count), ex=self._counts_ttl_seconds)
        return count

    async def _count_nearby_api(
        self, *, query: str, lat: float, lon: float
    ) -> int | None:
        params: dict[str, str | int] = {
            "q": query,
            "point": f"{lon},{lat}",
            "radius": self._radius_meters,
            "page_size": 1,
            "type": "branch",
            "fields": "items.id",
            "key": self._api_key,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(self._items_url, params=params)
                response.raise_for_status()
        except httpx.HTTPError:
            logger.warning("2GIS nearby count request failed query=%s", query)
            return None

        try:
            data = response.json()
        except ValueError:
            logger.warning("2GIS nearby count returned invalid JSON query=%s", query)
            return None
        total = data.get("result", {}).get("total")
        if type(total) is int and total >= 0:
            return total
        logger.warning("2GIS nearby count missing valid total query=%s", query)
        return None
