"""Preference profile learned from a user's saved / rejected apartments.

/foryou ranks fresh search candidates by how well they match what the user has
saved (positive signal) and away from what they rejected (negative signal). It is
deterministic and explainable — no extra LLM call — so each recommendation can
say *why* it fits the user's taste.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.models.enriched import EnrichedApartment
from agent.tools.districts import canonical_district

# Tolerance around the saved budget / area range before a candidate counts as a fit.
_RANGE_SLACK = 0.1


@dataclass(slots=True)
class PreferenceProfile:
    """What the user tends to like, derived from their saved apartments."""

    saved_count: int = 0
    liked_districts: set[str] = field(default_factory=set)
    disliked_districts: set[str] = field(default_factory=set)
    price_lo: int | None = None
    price_hi: int | None = None
    area_lo: float | None = None
    area_hi: float | None = None
    rooms: set[int] = field(default_factory=set)

    @property
    def has_signal(self) -> bool:
        return self.saved_count > 0


def _district(item: EnrichedApartment) -> str | None:
    return canonical_district(item.apartment.district, item.apartment.city)


def build_preference_profile(
    saved: list[EnrichedApartment],
    rejected: list[EnrichedApartment],
) -> PreferenceProfile:
    """Summarize taste from saved (liked) and rejected (disliked) apartments."""
    liked_districts = {d for item in saved if (d := _district(item)) is not None}
    disliked_districts = {
        d
        for item in rejected
        if (d := _district(item)) is not None and d not in liked_districts
    }
    prices = [item.apartment.price_kzt for item in saved if item.apartment.price_kzt]
    areas = [item.apartment.area_m2 for item in saved if item.apartment.area_m2]
    rooms = {item.apartment.rooms for item in saved if item.apartment.rooms is not None}
    return PreferenceProfile(
        saved_count=len(saved),
        liked_districts=liked_districts,
        disliked_districts=disliked_districts,
        price_lo=min(prices) if prices else None,
        price_hi=max(prices) if prices else None,
        area_lo=min(areas) if areas else None,
        area_hi=max(areas) if areas else None,
        rooms=rooms,
    )


def score_candidate(
    item: EnrichedApartment, profile: PreferenceProfile
) -> tuple[float, list[str]]:
    """Return a preference-fit score and the human reasons behind it."""
    score = 0.0
    reasons: list[str] = []
    apartment = item.apartment

    district = _district(item)
    if district is not None and district in profile.liked_districts:
        score += 2.0
        reasons.append(f"район как в сохранённых ({district})")
    elif district is not None and district in profile.disliked_districts:
        score -= 2.0

    if apartment.rooms is not None and apartment.rooms in profile.rooms:
        score += 1.0
        reasons.append(f"{apartment.rooms}-комн. — как вы сохраняли")

    if (
        apartment.price_kzt is not None
        and profile.price_lo is not None
        and profile.price_hi is not None
        and profile.price_lo * (1 - _RANGE_SLACK)
        <= apartment.price_kzt
        <= profile.price_hi * (1 + _RANGE_SLACK)
    ):
        score += 1.0
        reasons.append("бюджет в вашем диапазоне")

    if (
        apartment.area_m2 is not None
        and profile.area_lo is not None
        and profile.area_hi is not None
        and profile.area_lo * (1 - _RANGE_SLACK)
        <= apartment.area_m2
        <= profile.area_hi * (1 + _RANGE_SLACK)
    ):
        score += 1.0
        reasons.append("похожая площадь")

    return score, reasons


def rank_by_preference(
    candidates: list[EnrichedApartment], profile: PreferenceProfile
) -> list[tuple[EnrichedApartment, list[str]]]:
    """Order candidates by preference fit (ties broken by the objective score)."""
    scored: list[tuple[float, float, int, EnrichedApartment, list[str]]] = []
    for position, item in enumerate(candidates):
        fit, reasons = score_candidate(item, profile)
        objective = item.score.score if item.score is not None else 0.0
        # position keeps the sort stable and total-orderable across ties.
        scored.append((fit, objective, -position, item, reasons))
    scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    return [(item, reasons) for _, _, _, item, reasons in scored]
