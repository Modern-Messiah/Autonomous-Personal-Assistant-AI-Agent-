"""Canonical city-district resolution shared by intent parsing and the parser.

krisha ignores server-side district params, so districts are filtered client-side
by comparing the requested district to a listing's Russian district label. Both
sides are reduced to a canonical name *scoped to the city* (district names repeat
across cities — e.g. Astana also has an "Алматинский" district) via the table
below. Only official city districts are covered; microdistricts are out of scope.

Aliases are matched as case-insensitive substrings, so a stem covers Russian
declensions ("бостандыкский" ⊃ "бостандык") and both scripts. Add a city or a
district by extending CITY_DISTRICTS — an unknown city/district resolves to None,
which callers treat as "not filterable" (keep the listing), never a wrong drop.
"""

from __future__ import annotations

CITY_DISTRICTS: dict[str, dict[str, str]] = {
    "Almaty": {
        "alatau": "Alatau", "алатау": "Alatau",
        "almaly": "Almaly", "алмалин": "Almaly", "алмалы": "Almaly",
        "auezov": "Auezov", "ауэзов": "Auezov", "ауезов": "Auezov",
        "bostandyk": "Bostandyk", "бостандык": "Bostandyk",
        "medeu": "Medeu", "медеу": "Medeu",
        "nauryzbay": "Nauryzbay", "наурызбай": "Nauryzbay",
        "turksib": "Turksib", "турксиб": "Turksib",
        "zhetysu": "Zhetysu", "жетысу": "Zhetysu",
    },
    "Astana": {
        "baikonyr": "Baikonyr", "baikonur": "Baikonyr",
        "байконыр": "Baikonyr", "байконур": "Baikonyr",
        "yesil": "Yesil", "esil": "Yesil", "есил": "Yesil",
        "saryarka": "Saryarka", "сарыарк": "Saryarka",
        "saraishyk": "Saraishyk", "сарайшык": "Saraishyk", "сарайшық": "Saraishyk",
        "nura": "Nura", "нура": "Nura", "нурин": "Nura",
        "almaty": "Almaty", "алматы": "Almaty", "алматин": "Almaty",
    },
    "Shymkent": {
        "abay": "Abay", "абай": "Abay",
        "al-farabi": "Al-Farabi", "alfarabi": "Al-Farabi", "фараби": "Al-Farabi",
        "yenbekshi": "Yenbekshi", "enbekshi": "Yenbekshi", "енбекши": "Yenbekshi",
        "karatau": "Karatau", "каратау": "Karatau",
        "turan": "Turan", "туран": "Turan",
    },
    "Karaganda": {
        "kazybek": "Kazybek bi", "казыбек": "Kazybek bi",
        "oktyabr": "Oktyabrsky", "октябр": "Oktyabrsky",
    },
}


def canonical_district(text: str | None, city: str | None) -> str | None:
    """Resolve a district label/name to its canonical form within ``city``.

    Returns None when the text or city is unknown, so callers can treat the
    listing as not filterable (keep it) instead of dropping it by mistake.
    """
    if not text or not city:
        return None
    aliases = CITY_DISTRICTS.get(city)
    if aliases is None:
        return None
    lowered = text.lower()
    for alias, canonical in aliases.items():
        if alias in lowered:
            return canonical
    return None


# Aliases that double as city names — valid as city-scoped districts (Astana has
# an "Алматы" district), but excluded from the city-agnostic union so that
# "квартира в Алматы" (the city) is not misread as a district during intent parsing.
_CITY_NAME_TOKENS = {"almaty", "алматы", "astana", "астана", "shymkent", "шымкент"}


def flat_district_aliases() -> dict[str, str]:
    """Union of every (alias -> canonical) pair, for city-agnostic normalization.

    Used by intent parsing, where the city is not yet bound; the authoritative,
    city-scoped match happens later via :func:`canonical_district`. City-name
    tokens are skipped (see above) and on a collision the first city listed in
    CITY_DISTRICTS wins.
    """
    merged: dict[str, str] = {}
    for aliases in CITY_DISTRICTS.values():
        for alias, canonical in aliases.items():
            if alias in _CITY_NAME_TOKENS:
                continue
            merged.setdefault(alias, canonical)
    return merged
