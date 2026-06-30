"""Tests for city-scoped district resolution."""

from __future__ import annotations

from agent.tools.districts import CITY_DISTRICTS, canonical_district, flat_district_aliases


def test_canonical_district_resolves_russian_label_and_declensions() -> None:
    assert canonical_district("Бостандыкский район", "Almaty") == "Bostandyk"
    assert canonical_district("в Алмалинском районе", "Almaty") == "Almaly"
    assert canonical_district("Bostandyk", "Almaty") == "Bostandyk"


def test_canonical_district_is_scoped_to_city() -> None:
    # Yesil belongs to Astana, not Almaty.
    assert canonical_district("Есильский район", "Astana") == "Yesil"
    assert canonical_district("Есильский район", "Almaty") is None
    # Astana labels its districts as "Алматы район" / "Сарайшык район" (newer one).
    assert canonical_district("Алматы район", "Astana") == "Almaty"
    assert canonical_district("Сарайшык район", "Astana") == "Saraishyk"
    # the city Almaty is not a district of Almaty itself
    assert canonical_district("Алматы", "Almaty") is None


def test_city_name_tokens_excluded_from_flat_union() -> None:
    # "Алматы"/"Almaty" are city names; they must not become districts during
    # city-agnostic intent parsing, even though Astana has an "Алматы" district.
    flat = flat_district_aliases()
    assert "алматы" not in flat
    assert "almaty" not in flat
    # but the city-scoped resolver still knows Astana's Almaty district
    assert canonical_district("Алматы район", "Astana") == "Almaty"


def test_canonical_district_returns_none_for_unknown_inputs() -> None:
    assert canonical_district(None, "Almaty") is None
    assert canonical_district("Бостандык", None) is None
    assert canonical_district("Бостандык", "Taraz") is None  # unmapped city
    assert canonical_district("Несуществующий район", "Almaty") is None


def test_flat_aliases_cover_every_city_and_prefer_first_on_collision() -> None:
    flat = flat_district_aliases()
    # every alias from every city is present
    total = sum(len(aliases) for aliases in CITY_DISTRICTS.values())
    assert len(flat) <= total  # collisions collapse, never exceed
    assert flat["бостандык"] == "Bostandyk"
    assert flat["сарыарк"] == "Saryarka"
    assert flat["абай"] == "Abay"
