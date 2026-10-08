"""Tests for enrichment node and mortgage utilities."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from agent.graph import run_search_graph
from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.nodes.enrich_node import EnrichNode
from agent.nodes.search_node import SearchNode
from agent.tools.mortgage import calculate_annuity_payment
from agent.tools.two_gis_client import NearbySummary


class FakeSearchParser:
    """Fake parser used to supply deterministic apartments."""

    def __init__(self, apartments: list[Apartment]) -> None:
        self._apartments = apartments

    async def search(self, context: object, criteria: SearchCriteria) -> list[Apartment]:
        del context, criteria
        return self._apartments


class FakeAreaClient:
    """Stub area client for tests."""

    def __init__(self, summary: NearbySummary | None) -> None:
        self._summary = summary

    async def get_nearby_summary(self, *, city: str, address: str) -> NearbySummary | None:
        del city, address
        return self._summary

    async def get_nearby_summary_at(
        self, *, city: str, lat: float, lon: float
    ) -> NearbySummary | None:
        del city, lat, lon
        return self._summary


class BrokenAreaClient:
    """Failing area client to test graceful fallbacks."""

    async def get_nearby_summary(self, *, city: str, address: str) -> NearbySummary | None:
        del city, address
        raise RuntimeError("area client unavailable")

    async def get_nearby_summary_at(
        self, *, city: str, lat: float, lon: float
    ) -> NearbySummary | None:
        del city, lat, lon
        raise RuntimeError("area client unavailable")


class RecordingAreaClient:
    """Records which entrypoint the enrichment chose."""

    def __init__(self) -> None:
        self.geocoded: list[tuple[str, str]] = []
        self.at_points: list[tuple[str, float, float]] = []

    async def get_nearby_summary(self, *, city: str, address: str) -> NearbySummary | None:
        self.geocoded.append((city, address))
        return None

    async def get_nearby_summary_at(
        self, *, city: str, lat: float, lon: float
    ) -> NearbySummary | None:
        self.at_points.append((city, lat, lon))
        return None


class FakeRateProvider:
    """Fixed rate provider for deterministic mortgage results."""

    def __init__(self, annual_rate: float) -> None:
        self._annual_rate = annual_rate

    async def get_annual_rate(self) -> float:
        return self._annual_rate


class BrokenRateProvider:
    """Failing rate provider to test graceful fallbacks."""

    async def get_annual_rate(self) -> float:
        raise RuntimeError("rate source unavailable")


def make_context_factory(context: object):
    @asynccontextmanager
    async def factory():
        yield context

    return factory


def build_criteria() -> SearchCriteria:
    return SearchCriteria(
        user_id=101,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        page_limit=1,
    )


def build_apartment(
    *,
    with_address: bool = True,
    coordinates: tuple[float, float] | None = None,
) -> Apartment:
    return Apartment(
        external_id="300400",
        source="krisha",
        url="https://krisha.kz/a/show/300400",
        title="Apartment for enrich test",
        price_kzt=40_000_000,
        city="Almaty",
        address="Satpayev 1" if with_address else None,
        rooms=2,
        area_m2=60.0,
        floor="7/12",
        photos=["https://photos.krisha.kz/300400/1.jpg"],
        published_at=datetime(2025, 2, 1, tzinfo=UTC),
        latitude=coordinates[0] if coordinates else None,
        longitude=coordinates[1] if coordinates else None,
    )


def test_calculate_annuity_payment() -> None:
    monthly, overpayment = calculate_annuity_payment(
        principal_kzt=32_000_000,
        annual_rate_percent=16.0,
        years=20,
    )
    assert monthly > 0
    assert overpayment > 0


@pytest.mark.asyncio
async def test_enrich_node_adds_area_and_mortgage_data() -> None:
    apartment = build_apartment(with_address=True)
    node = EnrichNode(
        area_client=FakeAreaClient(NearbySummary(schools=8, parks=5, metro=2)),
        interest_rate_provider=FakeRateProvider(annual_rate=15.0),
    )

    result = await node({"criteria": build_criteria(), "apartments": [apartment]})
    enriched = result["enriched_apartments"][0]

    assert enriched.nearby_schools == 8
    assert enriched.nearby_parks == 5
    assert enriched.nearby_metro == 2
    assert enriched.mortgage_monthly_payment_kzt is not None
    assert enriched.mortgage_monthly_payment_kzt > 0
    assert enriched.mortgage_total_overpayment_kzt is not None
    assert enriched.mortgage_total_overpayment_kzt > 0


@pytest.mark.asyncio
async def test_enrich_node_skips_mortgage_for_rent() -> None:
    # A rental price is a monthly rate; a "mortgage" estimate from it is nonsense.
    apartment = build_apartment(with_address=True)
    node = EnrichNode(
        area_client=FakeAreaClient(NearbySummary(schools=8, parks=5, metro=2)),
        interest_rate_provider=FakeRateProvider(annual_rate=15.0),
    )
    rent_criteria = build_criteria().model_copy(update={"deal_type": "rent"})

    result = await node({"criteria": rent_criteria, "apartments": [apartment]})
    enriched = result["enriched_apartments"][0]

    assert enriched.mortgage_monthly_payment_kzt is None
    assert enriched.mortgage_total_overpayment_kzt is None
    assert enriched.nearby_schools == 8  # nearby enrichment still applies


@pytest.mark.asyncio
async def test_enrich_node_falls_back_on_provider_errors() -> None:
    apartment = build_apartment(with_address=True)
    node = EnrichNode(
        area_client=BrokenAreaClient(),
        interest_rate_provider=BrokenRateProvider(),
    )

    result = await node({"criteria": build_criteria(), "apartments": [apartment]})
    enriched = result["enriched_apartments"][0]

    assert enriched.nearby_schools is None
    assert enriched.nearby_parks is None
    assert enriched.nearby_metro is None
    assert enriched.mortgage_monthly_payment_kzt is None
    assert enriched.mortgage_total_overpayment_kzt is None


@pytest.mark.asyncio
async def test_run_search_graph_uses_enrich_node_when_provided() -> None:
    apartment = build_apartment(with_address=True)
    search_node = SearchNode(
        parser=FakeSearchParser([apartment]),
        context_factory=make_context_factory(object()),
    )
    enrich_node = EnrichNode(
        area_client=FakeAreaClient(NearbySummary(schools=4, parks=3, metro=1)),
        interest_rate_provider=FakeRateProvider(annual_rate=14.0),
    )

    enriched = await run_search_graph(
        build_criteria(),
        search_node=search_node,
        enrich_node=enrich_node,
    )

    assert len(enriched) == 1
    assert enriched[0].apartment.external_id == "300400"
    assert enriched[0].nearby_schools == 4
    assert enriched[0].nearby_metro == 1


@pytest.mark.asyncio
async def test_listing_map_pin_skips_geocoding_entirely() -> None:
    from agent.nodes.enrich_node import EnrichNode

    recorder = RecordingAreaClient()
    node = EnrichNode(area_client=recorder)

    apartment = build_apartment(coordinates=(43.2927, 77.0140))
    await node._enrich_apartment(apartment, deal_type="sale")

    # the listing's own pin wins: no geocode call, one exact-point call
    assert recorder.geocoded == []
    assert recorder.at_points == [("Almaty", 43.2927, 77.0140)]


@pytest.mark.asyncio
async def test_address_geocoding_remains_the_fallback() -> None:
    from agent.nodes.enrich_node import EnrichNode

    recorder = RecordingAreaClient()
    node = EnrichNode(area_client=recorder)

    await node._enrich_apartment(build_apartment(), deal_type="sale")

    assert recorder.at_points == []
    assert recorder.geocoded == [("Almaty", "Satpayev 1")]
