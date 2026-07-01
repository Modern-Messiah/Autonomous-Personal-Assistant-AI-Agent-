"""Preference profile learned from a user's saved / rejected apartments.

/foryou ranks fresh search candidates by how well they match what the user has
saved (positive signal) and away from what they rejected (negative signal). It is
deterministic and explainable — no extra LLM call — so each recommendation can
say *why* it fits the user's taste.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.tools.districts import canonical_district

# Tolerance around the saved budget / area range before a candidate counts as a fit.
_RANGE_SLACK = 0.1
# How much the criteria-aware objective score (0-100) weighs in the final order,
# relative to taste-fit points. The objective score already judges fit to the
# search criteria (budget/rooms/area), so folding it into the primary key — not
# only tie-breaks — is what makes /foryou weigh the active criteria, not just taste.
_OBJECTIVE_WEIGHT = 3.0


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


def criteria_fit(
    item: EnrichedApartment, criteria: SearchCriteria | None
) -> tuple[float, list[str]]:
    """Reward a candidate for matching the *active search criteria*, not just taste.

    Candidates already pass the criteria hard-filters, so most of these points are
    uniform; their job is the explanation ("в нужном районе, в рамках бюджета") plus
    a small value tilt toward cheaper-within-budget listings so the criteria — not
    only learned taste — shape the order.
    """
    if criteria is None:
        return 0.0, []
    score = 0.0
    reasons: list[str] = []
    apartment = item.apartment

    wanted = {
        district
        for name in (criteria.districts or ())
        if (district := canonical_district(name, criteria.city)) is not None
    }
    if wanted and (found := _district(item)) is not None and found in wanted:
        score += 0.5
        reasons.append(f"в нужном районе ({found})")

    if criteria.rooms and apartment.rooms is not None and apartment.rooms in criteria.rooms:
        score += 0.5
        reasons.append("число комнат по запросу")

    if (
        criteria.max_price_kzt is not None
        and apartment.price_kzt is not None
        and apartment.price_kzt <= criteria.max_price_kzt
    ):
        headroom = (criteria.max_price_kzt - apartment.price_kzt) / criteria.max_price_kzt
        # base fit + value tilt (cheaper within budget ranks a little higher)
        score += 0.5 + max(0.0, min(headroom, 1.0)) * 0.5
        reasons.append("в рамках бюджета")

    return score, reasons


def rank_by_preference(
    candidates: list[EnrichedApartment],
    profile: PreferenceProfile,
    criteria: SearchCriteria | None = None,
) -> list[tuple[EnrichedApartment, list[str]]]:
    """Order candidates by taste fit + active-criteria fit + criteria-aware score.

    The primary key blends three signals: learned taste (saved/rejected), how well
    the candidate matches the active search criteria, and the objective score (which
    is itself criteria-aware). Criteria reasons come first in the explanation.
    """
    scored: list[tuple[float, float, int, EnrichedApartment, list[str]]] = []
    for position, item in enumerate(candidates):
        fit, taste_reasons = score_candidate(item, profile)
        crit, crit_reasons = criteria_fit(item, criteria)
        objective = item.score.score if item.score is not None else 0.0
        primary = fit + crit + _OBJECTIVE_WEIGHT * (objective / 100.0)
        reasons = (crit_reasons + taste_reasons)[:4]
        # position keeps the sort stable and total-orderable across ties.
        scored.append((primary, objective, -position, item, reasons))
    scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    return [(item, reasons) for _, _, _, item, reasons in scored]
