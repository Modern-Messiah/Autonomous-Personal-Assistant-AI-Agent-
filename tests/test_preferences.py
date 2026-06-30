"""Tests for the preference profile used by /foryou."""

from __future__ import annotations

from agent.models.apartment import Apartment
from agent.models.enriched import EnrichedApartment
from agent.models.score import ApartmentScore
from bot.preferences import build_preference_profile, rank_by_preference, score_candidate


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
