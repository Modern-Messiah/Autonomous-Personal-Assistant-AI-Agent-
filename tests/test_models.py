"""Tests for Pydantic domain models."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.score import ApartmentScore


def test_search_criteria_accepts_valid_data() -> None:
    criteria = SearchCriteria(
        user_id=1,
        city="Almaty",
        deal_type="sale",
        min_price_kzt=20_000_000,
        max_price_kzt=40_000_000,
        rooms=[2, 3, 2],
        districts=["Bostandyk", " Medeu "],
        min_area_m2=45.0,
        max_area_m2=90.0,
    )
    assert criteria.page_limit == 3
    assert criteria.rooms == [2, 3]
    assert criteria.districts == ["Bostandyk", "Medeu"]


def test_search_criteria_rejects_invalid_price_range() -> None:
    with pytest.raises(ValidationError):
        SearchCriteria(
            user_id=1,
            city="Almaty",
            deal_type="sale",
            min_price_kzt=50_000_000,
            max_price_kzt=40_000_000,
        )


def test_search_criteria_rejects_invalid_area_range() -> None:
    with pytest.raises(ValidationError):
        SearchCriteria(
            user_id=1,
            city="Almaty",
            deal_type="sale",
            min_area_m2=80.0,
            max_area_m2=70.0,
        )


def test_apartment_requires_valid_urls() -> None:
    with pytest.raises(ValidationError):
        Apartment(
            external_id="abc",
            source="krisha",
            url="invalid-url",
            title="Apartment",
            price_kzt=30_000_000,
            city="Almaty",
            photos=[],
        )


def test_apartment_score_bounds() -> None:
    ApartmentScore(score=100, reasons=["great district"], recommendation="strong_buy")

    with pytest.raises(ValidationError):
        ApartmentScore(score=101, reasons=["too high"], recommendation="skip")


def test_apartment_defaults_scraped_at() -> None:
    apartment = Apartment(
        external_id="kr-1",
        source="krisha",
        url="https://krisha.kz/a/show/1",
        title="Cozy apartment",
        price_kzt=25_000_000,
        city="Almaty",
        photos=["https://krisha.kz/images/1.jpg"],
        published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert apartment.scraped_at.tzinfo is not None

