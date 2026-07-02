"""Tests for the preference profile used by /foryou."""

from __future__ import annotations

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.models.score import ApartmentScore
from bot.preferences import (
    build_preference_profile,
    build_taste_criteria,
    rank_by_preference,
    score_candidate,
)


def apt(
    *,
    ext: str,
    city: str = "Almaty",
    district: str | None = None,
    price: int = 30_000_000,
    area: float = 55.0,
    rooms: int = 2,
    score: float | None = None,
) -> EnrichedApartment:
    return EnrichedApartment(
        apartment=Apartment(
            external_id=ext,
            source="krisha",
            url=f"https://krisha.kz/a/show/{ext}",
            title=f"Apartment {ext}",
            price_kzt=price,
            city=city,
            district=district,
            area_m2=area,
            rooms=rooms,
            photos=[],
        ),
        score=(
            ApartmentScore(score=score, reasons=["x"], recommendation="consider")
            if score is not None
            else None
        ),
    )


def test_profile_learns_districts_price_area_rooms() -> None:
    profile = build_preference_profile(
        [apt(ext="1", district="Бостандыкский район", price=30_000_000, area=55, rooms=2)],
        [],
    )
    assert profile.has_signal
    assert profile.liked_districts == {"Bostandyk"}
    assert profile.price_lo == 30_000_000
    assert profile.rooms == {2}


def test_profile_uses_city_scoped_catalog_outside_almaty() -> None:
    profile = build_preference_profile(
        [apt(ext="1", city="Aktobe", district="Алматинский район")],
        [],
    )

    assert profile.liked_districts == {"Almaty"}


def test_empty_profile_has_no_signal() -> None:
    assert not build_preference_profile([], []).has_signal


def test_rank_prefers_saved_district() -> None:
    profile = build_preference_profile(
        [apt(ext="1", district="Бостандыкский район", price=30_000_000, area=55, rooms=2)],
        [],
    )
    medeu = apt(ext="11", district="Медеуский район", price=80_000_000, area=120, rooms=4)
    bostandyk = apt(ext="10", district="Бостандыкский район", price=31_000_000, area=54, rooms=2)

    ranked = rank_by_preference([medeu, bostandyk], profile)

    assert ranked[0][0].apartment.external_id == "10"  # matching district wins
    assert any("Bostandyk" in reason for reason in ranked[0][1])


def test_rejected_district_is_penalized() -> None:
    profile = build_preference_profile(
        [apt(ext="1", district="Бостандыкский район")],
        [apt(ext="2", district="Алмалинский район")],
    )
    assert profile.disliked_districts == {"Almaly"}
    # Differ on price/area/rooms so only the disliked-district signal applies.
    candidate = apt(ext="10", district="Алмалинский район", price=99_000_000, area=200, rooms=5)
    fit, _ = score_candidate(candidate, profile)
    assert fit < 0


def test_rank_breaks_ties_by_objective_score() -> None:
    # Two candidates with identical preference fit; the higher objective score wins.
    profile = build_preference_profile([apt(ext="1", district="Медеуский район")], [])
    low = apt(ext="20", district="Бостандыкский район", score=40.0)
    high = apt(ext="21", district="Бостандыкский район", score=90.0)
    ranked = rank_by_preference([low, high], profile)
    assert ranked[0][0].apartment.external_id == "21"


def test_taste_criteria_searches_what_the_user_saves() -> None:
    # User saves Almaty purchases (~40M, Auezov/Alatau) but last searched RENT:
    # /foryou must search the saved taste, not rerank the rent results.
    saved = [
        apt(ext="1", district="Ауэзовский район", price=40_000_000, area=60, rooms=2),
        apt(ext="2", district="Алатауский район", price=41_000_000, area=73, rooms=2),
    ]
    profile = build_preference_profile(saved, [])
    base = SearchCriteria(
        user_id=77, city="Almaty", deal_type="rent", property_type="apartment",
        max_price_kzt=500_000, rooms=[2], page_limit=3,
    )

    taste = build_taste_criteria(profile, saved, base=base)

    assert taste.city == "Almaty"
    assert taste.deal_type == "sale"  # inferred: 41M is a purchase, not a rent
    assert sorted(taste.districts or []) == ["Alatau", "Auezov"]
    assert taste.rooms == [2]
    assert taste.min_price_kzt == int(40_000_000 * 0.85)
    assert taste.max_price_kzt == int(41_000_000 * 1.15)
    assert taste.user_id == 77 and taste.page_limit == 3  # anchored to base


def test_taste_criteria_infers_rent_and_falls_back_to_base() -> None:
    # Saved rentals (300K/mo, no district) -> rent inferred, districts fall back.
    saved = [apt(ext="1", district=None, price=300_000, area=45, rooms=1)]
    profile = build_preference_profile(saved, [])
    base = SearchCriteria(
        user_id=1, city="Astana", deal_type="sale", property_type="apartment",
        page_limit=3,
    )

    taste = build_taste_criteria(profile, saved, base=base)

    assert taste.deal_type == "rent"
    assert taste.districts is None  # no liked districts -> whole city
    assert taste.rooms == [1]
    assert taste.min_price_kzt == int(300_000 * 0.85)


def test_rank_uses_active_criteria_district_and_budget() -> None:
    # Taste is identical for both candidates (neither district is liked/disliked,
    # same rooms/price/area); the active criteria break the tie toward the
    # requested district + in-budget listing, and explain why.
    profile = build_preference_profile(
        [apt(ext="1", district="Турксибский район", price=30_000_000, area=55, rooms=2)],
        [],
    )
    criteria = SearchCriteria(
        user_id=1,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        max_price_kzt=45_000_000,
        rooms=[2],
        districts=["Medeu"],
    )
    medeu = apt(ext="10", district="Медеуский район", price=30_000_000, area=55, rooms=2)
    almaly = apt(ext="11", district="Алмалинский район", price=30_000_000, area=55, rooms=2)

    ranked = rank_by_preference([almaly, medeu], profile, criteria=criteria)

    assert ranked[0][0].apartment.external_id == "10"  # requested district wins the tie
    assert any("Medeu" in reason for reason in ranked[0][1])
    assert any("бюджет" in reason for reason in ranked[0][1])
