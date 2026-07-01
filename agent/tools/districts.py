"""Compatibility helpers backed by the shared Kazakhstan location catalog."""

from __future__ import annotations

from agent.locations import LOCATIONS, normalize_location_text


def canonical_district(text: str | None, city: str | None) -> str | None:
    """Resolve a district label within one city."""
    return LOCATIONS.canonical_district(text, city)


def flat_district_aliases() -> dict[str, str]:
    """Return district aliases that are unambiguous without a city."""
    aliases = LOCATIONS.unambiguous_district_aliases()
    city_tokens = {
        normalize_location_text(alias)
        for city in LOCATIONS.cities
        for alias in {city.canonical, city.name_ru, city.name_kk, *city.aliases}
    }
    return {alias: canonical for alias, canonical in aliases.items() if alias not in city_tokens}
