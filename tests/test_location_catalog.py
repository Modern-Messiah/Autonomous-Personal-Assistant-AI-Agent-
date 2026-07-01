"""Integrity and lookup tests for the Kazakhstan location catalog."""

from __future__ import annotations

import pytest

from agent.locations import LOCATIONS


def test_catalog_contains_pinned_90_official_cities() -> None:
    assert LOCATIONS.metadata.kato_version == "\u041d\u041a \u0420\u041a 11-2025"
    assert LOCATIONS.metadata.updated_at == "2026-06-18"
    assert len(LOCATIONS.cities) == 90
    assert len({city.kato_code for city in LOCATIONS.cities}) == 90


def test_catalog_marks_only_zhem_as_unavailable_on_krisha() -> None:
    unavailable = [city for city in LOCATIONS.cities if city.krisha_slug is None]

    assert [(city.canonical, city.name_ru) for city in unavailable] == [("Zhem", "Жем")]
    assert len({city.krisha_slug for city in LOCATIONS.cities if city.krisha_slug}) == 89


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Конаев", "Konaev"),
        ("Қонаев", "Konaev"),
        ("Капчагай", "Konaev"),
        ("в Конаеве", "Konaev"),
        ("Усть-Каменогорск", "Ust-Kamenogorsk"),
        ("Өскемен", "Ust-Kamenogorsk"),
        ("Щучинск", "Shchuchinsk"),
        ("Жем", "Zhem"),
    ],
)
def test_catalog_resolves_official_and_historical_city_names(
    raw: str,
    canonical: str,
) -> None:
    assert LOCATIONS.canonical_city(raw) == canonical


def test_every_city_official_name_resolves_to_itself() -> None:
    for city in LOCATIONS.cities:
        assert LOCATIONS.canonical_city(city.name_ru) == city.canonical
        assert LOCATIONS.canonical_city(city.name_kk) == city.canonical


def test_district_lookup_is_scoped_to_city() -> None:
    assert LOCATIONS.canonical_district("Алматинский район", "Astana") == "Almaty"
    assert LOCATIONS.canonical_district("Алмалинский район", "Almaty") == "Almaly"
    assert LOCATIONS.canonical_district("Бостандыкский", "Astana") is None


def test_catalog_contains_all_official_city_districts() -> None:
    counts = {
        city.canonical: len(city.districts)
        for city in LOCATIONS.cities
        if city.districts
    }

    assert counts == {
        "Aktobe": 2,
        "Almaty": 8,
        "Astana": 6,
        "Karaganda": 2,
        "Shymkent": 5,
        "Taraz": 2,
    }
    assert sum(counts.values()) == 25


def test_city_without_city_districts_has_empty_district_list() -> None:
    assert LOCATIONS.districts_for_city("Konaev") == ()


def test_every_district_resolves_only_under_its_parent_city() -> None:
    for city in LOCATIONS.cities:
        for district in city.districts:
            assert (
                LOCATIONS.canonical_district(district.name_ru, city.canonical)
                == district.canonical
            )
            assert (
                LOCATIONS.canonical_district(district.name_kk, city.canonical)
                == district.canonical
            )
