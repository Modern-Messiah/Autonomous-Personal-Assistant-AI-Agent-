"""Request-level city and district resolution tests."""

from __future__ import annotations

import pytest

from agent.locations import LocationInputError, resolve_locations


def test_city_only_search_has_no_district_filter() -> None:
    result = resolve_locations(message="квартира в Конаеве", default_city="Almaty")

    assert result.city == "Konaev"
    assert result.districts is None
    assert result.defaulted_city is False


def test_city_and_valid_district_are_canonicalized() -> None:
    result = resolve_locations(
        message="квартира в Астане, Есильский район",
        default_city="Almaty",
    )

    assert result.city == "Astana"
    assert result.districts == ("Yesil",)


def test_unique_district_can_infer_city() -> None:
    result = resolve_locations(
        message="квартира в Бостандыкском районе",
        default_city="Astana",
    )

    assert result.city == "Almaty"
    assert result.districts == ("Bostandyk",)
    assert result.defaulted_city is False


def test_mismatched_district_is_rejected() -> None:
    with pytest.raises(LocationInputError, match="не относится"):
        resolve_locations(
            message="квартира в Астане",
            llm_districts=["Бостандыкский район"],
            default_city="Almaty",
        )


def test_mismatched_known_district_is_rejected_without_llm() -> None:
    with pytest.raises(LocationInputError, match="не относится"):
        resolve_locations(
            message="квартира в Астане, Бостандыкский район",
            default_city="Almaty",
        )


def test_ambiguous_district_without_city_is_rejected() -> None:
    with pytest.raises(LocationInputError, match="укажи город"):
        resolve_locations(
            message="квартира",
            llm_districts=["район Алматы"],
            default_city="Almaty",
        )


def test_invalid_llm_city_is_not_silently_defaulted() -> None:
    with pytest.raises(LocationInputError, match="Город"):
        resolve_locations(
            message="квартира",
            llm_city="Несуществующий",
            default_city="Almaty",
        )


def test_zhem_is_recognized_but_reports_krisha_limitation() -> None:
    with pytest.raises(LocationInputError, match="Krisha"):
        resolve_locations(message="квартира в Жеме", default_city="Almaty")


def test_missing_location_uses_default_city() -> None:
    result = resolve_locations(message="двухкомнатная до 30 млн", default_city="Almaty")

    assert result.city == "Almaty"
    assert result.districts is None
    assert result.defaulted_city is True


def test_refinement_keeps_existing_location_when_location_is_not_changed() -> None:
    result = resolve_locations(
        message="только 3 комнаты",
        default_city="Almaty",
        existing_city="Astana",
        existing_districts=["Yesil"],
    )

    assert result.city == "Astana"
    assert result.districts == ("Yesil",)


def test_refinement_clears_old_district_when_city_changes() -> None:
    result = resolve_locations(
        message="теперь в Конаеве",
        default_city="Almaty",
        existing_city="Astana",
        existing_districts=["Yesil"],
    )

    assert result.city == "Konaev"
    assert result.districts is None
