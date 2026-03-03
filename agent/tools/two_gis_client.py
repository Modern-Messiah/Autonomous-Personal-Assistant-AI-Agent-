"""Minimal 2GIS client used by enrichment node."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(slots=True, frozen=True)
class NearbySummary:
    """Nearby infrastructure counts around listing location."""

    schools: int
    parks: int
    metro: int


class TwoGISClient:
    """HTTP client for fetching nearby places from 2GIS APIs."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 10.0,
        radius_meters: int = 2000,
    ) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._radius_meters = radius_meters
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
        params = {"q": f"{city}, {address}", "key": self._api_key}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
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

    async def _count_nearby(self, *, query: str, lat: float, lon: float) -> int:
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
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.get(self._items_url, params=params)
                response.raise_for_status()
        except httpx.HTTPError:
            return 0

        data = response.json()
        total = data.get("result", {}).get("total")
        if isinstance(total, int) and total >= 0:
            return total

        items = data.get("result", {}).get("items", [])
        if isinstance(items, list):
            return len(items)
        return 0
