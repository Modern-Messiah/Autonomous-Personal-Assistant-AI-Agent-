"""Enrichment node for search graph."""

from __future__ import annotations

import asyncio
from typing import Protocol, cast

from agent.models.apartment import Apartment
from agent.models.enriched import EnrichedApartment
from agent.nodes.search_node import SearchGraphState
from agent.tools.krisha_parser import build_redis_client
from agent.tools.mortgage import (
    InterestRateProviderProtocol,
    StaticInterestRateProvider,
    calculate_annuity_payment,
)
from agent.tools.osm_area_client import OsmAreaClient
from agent.tools.two_gis_client import NearbyCacheProtocol, NearbySummary, TwoGISClient
from config.settings import Settings, get_settings


class AreaClientProtocol(Protocol):
    """Contract for location-based enrichment providers."""

    async def get_nearby_summary(self, *, city: str, address: str) -> NearbySummary | None: ...

    async def get_nearby_summary_at(
        self, *, city: str, lat: float, lon: float
    ) -> NearbySummary | None: ...


class EnrichNode:
    """Adds area metadata and mortgage estimates to apartments."""

    def __init__(
        self,
        *,
        area_client: AreaClientProtocol | None = None,
        interest_rate_provider: InterestRateProviderProtocol | None = None,
        loan_to_value: float = 0.8,
        mortgage_years: int = 20,
    ) -> None:
        self._area_client = area_client
        self._interest_rate_provider = interest_rate_provider or StaticInterestRateProvider()
        self._loan_to_value = loan_to_value
        self._mortgage_years = mortgage_years

    async def __call__(self, state: SearchGraphState) -> SearchGraphState:
        apartments = state.get("apartments", [])
        criteria = state["criteria"]
        if not apartments:
            return {
                "criteria": criteria,
                "apartments": apartments,
                "enriched_apartments": [],
            }

        tasks = [
            self._enrich_apartment(apartment, deal_type=criteria.deal_type)
            for apartment in apartments
        ]
        enriched_apartments = await asyncio.gather(*tasks)
        return {
            "criteria": criteria,
            "apartments": apartments,
            "enriched_apartments": enriched_apartments,
        }

    async def _enrich_apartment(
        self, apartment: Apartment, *, deal_type: str = "sale"
    ) -> EnrichedApartment:
        area_task = asyncio.create_task(self._load_nearby_summary(apartment))
        nearby = await area_task
        # A mortgage estimate only makes sense for a purchase — on a rental the
        # price is a monthly rate, and "mortgage from 300 000 ₸" is nonsense.
        monthly_payment: int | None = None
        overpayment: int | None = None
        if deal_type != "rent":
            monthly_payment, overpayment = await self._calculate_mortgage(apartment.price_kzt)

        return EnrichedApartment(
            apartment=apartment,
            nearby_schools=None if nearby is None else nearby.schools,
            nearby_parks=None if nearby is None else nearby.parks,
            nearby_metro=None if nearby is None else nearby.metro,
            nearby_school_m=None if nearby is None else nearby.schools_nearest_m,
            nearby_park_m=None if nearby is None else nearby.parks_nearest_m,
            nearby_metro_m=None if nearby is None else nearby.metro_nearest_m,
            mortgage_monthly_payment_kzt=monthly_payment,
            mortgage_total_overpayment_kzt=overpayment,
        )

    async def _load_nearby_summary(self, apartment: Apartment) -> NearbySummary | None:
        if self._area_client is None:
            return None
        try:
            # The listing's own map pin beats every geocoder: krisha pins the
            # advert block in the embedded JSON, so prefer it whenever present
            # and fall back to address geocoding only for listings without it.
            if apartment.latitude is not None and apartment.longitude is not None:
                return await self._area_client.get_nearby_summary_at(
                    city=apartment.city,
                    lat=apartment.latitude,
                    lon=apartment.longitude,
                )
            if not apartment.address:
                return None
            return await self._area_client.get_nearby_summary(
                city=apartment.city, address=apartment.address
            )
        except Exception:
            return None

    async def _calculate_mortgage(self, price_kzt: int) -> tuple[int | None, int | None]:
        if price_kzt <= 0:
            return None, None
        try:
            annual_rate = await self._interest_rate_provider.get_annual_rate()
        except Exception:
            return None, None
        principal = int(price_kzt * self._loan_to_value)
        monthly_payment, overpayment = calculate_annuity_payment(
            principal_kzt=principal,
            annual_rate_percent=annual_rate,
            years=self._mortgage_years,
        )
        return monthly_payment, overpayment


def create_default_enrich_node(*, settings: Settings | None = None) -> EnrichNode:
    """Create enrich node with the configured area provider (None = settings).

    ``API__AREA_PROVIDER`` selects between the free OSM client (default) and
    the paid 2GIS one; both implement the same ``get_nearby_summary`` contract
    and share the Redis geocode/count caches.
    """
    settings = settings or get_settings()
    geocode_cache = cast(
        NearbyCacheProtocol,
        build_redis_client(settings.redis.redis_url),
    )
    if settings.api.area_provider == "2gis":
        two_gis_key = settings.api.two_gis_api_key
        if two_gis_key is None:
            msg = "API__AREA_PROVIDER=2gis requires API__TWO_GIS_API_KEY"
            raise ValueError(msg)
        area_client: AreaClientProtocol = TwoGISClient(
            api_key=two_gis_key.get_secret_value(),
            cache=geocode_cache,
        )
    else:
        area_client = OsmAreaClient(cache=geocode_cache)
    return EnrichNode(area_client=area_client, interest_rate_provider=StaticInterestRateProvider())
