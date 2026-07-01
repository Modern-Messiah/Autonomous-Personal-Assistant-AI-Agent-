"""Versioned Kazakhstan city and city-district catalog."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DASHES = str.maketrans(
    {"\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2011": "-"}
)
_NON_WORD = re.compile(r"[^\w-]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_LETTER_SUFFIX = r"[\w]{0,4}"


def normalize_location_text(value: str) -> str:
    """Normalize scripts and punctuation without transliterating names."""
    normalized = unicodedata.normalize("NFKC", value).translate(_DASHES).casefold()
    normalized = normalized.replace("\u0451", "\u0435")
    normalized = _NON_WORD.sub(" ", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()


def _alias_pattern(alias: str) -> re.Pattern[str]:
    normalized = normalize_location_text(alias)
    escaped = re.escape(normalized).replace(r"\ ", r"[\s-]+")
    # Long aliases may be followed by a short case ending. Short aliases such
    # as "Шу" must match exactly to avoid interpreting ordinary words as cities.
    suffix = _LETTER_SUFFIX if len(normalized.replace(" ", "")) >= 4 else ""
    return re.compile(rf"(?<!\w){escaped}{suffix}(?!\w)", re.IGNORECASE)


def _city_alias_pattern(alias: str) -> re.Pattern[str]:
    """Match a city plus common case endings, but never district adjectives."""
    normalized = normalize_location_text(alias)
    escaped = re.escape(normalized).replace(r"\ ", r"[\s-]+")
    suffix = (
        r"(?:\u0435|\u0430|\u0443|\u043e\u043c|\u0435\u043c|"
        r"\u044b|\u0438|\u044f|\u044e)?"
    )
    return re.compile(rf"(?<!\w){escaped}{suffix}(?!\w)", re.IGNORECASE)


def _alias_variants(alias: str) -> tuple[str, ...]:
    """Return an alias plus a Russian adjective stem when applicable."""
    normalized = normalize_location_text(alias)
    variants = [normalized]
    for ending in ("ский", "ская", "ское"):
        if normalized.endswith(ending):
            stem = normalized[: -len(ending)]
            if len(stem) >= 4:
                variants.append(stem)
            break
    return tuple(variants)


@dataclass(frozen=True, slots=True)
class CatalogMetadata:
    """Provenance of the official location snapshot."""

    kato_version: str
    updated_at: str
    source_url: str
    source_sha256: str


@dataclass(frozen=True, slots=True)
class District:
    """One official district inside a city."""

    kato_code: str
    canonical: str
    name_ru: str
    name_kk: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class City:
    """One official Kazakhstan city and its Krisha routing metadata."""

    kato_code: str
    canonical: str
    name_ru: str
    name_kk: str
    aliases: tuple[str, ...]
    krisha_slug: str | None
    districts: tuple[District, ...]


class LocationCatalog:
    """Validated immutable lookup table for cities and city districts."""

    def __init__(self, *, metadata: CatalogMetadata, cities: tuple[City, ...]) -> None:
        self.metadata = metadata
        self.cities = cities
        self._by_canonical = {city.canonical: city for city in cities}
        self._city_matchers = self._build_city_matchers()
        self._district_matchers = {
            city.canonical: self._build_district_matchers(city) for city in cities
        }
        self._validate()

    @classmethod
    def from_path(cls, path: Path) -> LocationCatalog:
        """Load and validate a JSON catalog."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        metadata_payload = payload["metadata"]
        metadata = CatalogMetadata(
            kato_version=metadata_payload["kato_version"],
            updated_at=metadata_payload["updated_at"],
            source_url=metadata_payload["source_url"],
            source_sha256=metadata_payload["source_sha256"],
        )
        cities = tuple(cls._city_from_payload(item) for item in payload["cities"])
        return cls(metadata=metadata, cities=cities)

    @staticmethod
    def _city_from_payload(payload: dict[str, Any]) -> City:
        districts = tuple(
            District(
                kato_code=item["kato_code"],
                canonical=item["canonical"],
                name_ru=item["name_ru"],
                name_kk=item["name_kk"],
                aliases=tuple(item["aliases"]),
            )
            for item in payload.get("districts", [])
        )
        return City(
            kato_code=payload["kato_code"],
            canonical=payload["canonical"],
            name_ru=payload["name_ru"],
            name_kk=payload["name_kk"],
            aliases=tuple(payload["aliases"]),
            krisha_slug=payload.get("krisha_slug"),
            districts=districts,
        )

    def _build_city_matchers(self) -> tuple[tuple[int, re.Pattern[str], str], ...]:
        matchers: list[tuple[int, re.Pattern[str], str]] = []
        for city in self.cities:
            aliases = {city.canonical, city.name_ru, city.name_kk, *city.aliases}
            for alias in aliases:
                for variant in _alias_variants(alias):
                    matchers.append(
                        (len(variant), _city_alias_pattern(variant), city.canonical)
                    )
        return tuple(sorted(matchers, key=lambda item: item[0], reverse=True))

    @staticmethod
    def _build_district_matchers(
        city: City,
    ) -> tuple[tuple[int, re.Pattern[str], str], ...]:
        matchers: list[tuple[int, re.Pattern[str], str]] = []
        for district in city.districts:
            aliases = {
                district.canonical,
                district.name_ru,
                district.name_kk,
                *district.aliases,
            }
            for alias in aliases:
                for variant in _alias_variants(alias):
                    matchers.append(
                        (len(variant), _alias_pattern(variant), district.canonical)
                    )
        return tuple(sorted(matchers, key=lambda item: item[0], reverse=True))

    def _validate(self) -> None:
        if len(self._by_canonical) != len(self.cities):
            raise ValueError("duplicate city canonical name in location catalog")
        if len({city.kato_code for city in self.cities}) != len(self.cities):
            raise ValueError("duplicate city KATO code in location catalog")
        searchable_slugs = [city.krisha_slug for city in self.cities if city.krisha_slug]
        if len(set(searchable_slugs)) != len(searchable_slugs):
            raise ValueError("duplicate Krisha city slug in location catalog")

        city_alias_owners: dict[str, str] = {}
        district_codes: set[str] = set()
        for city in self.cities:
            if not city.canonical or not city.name_ru or not city.name_kk:
                raise ValueError("blank city identity in location catalog")
            for alias in {city.canonical, city.name_ru, city.name_kk, *city.aliases}:
                normalized = normalize_location_text(alias)
                owner = city_alias_owners.setdefault(normalized, city.canonical)
                if owner != city.canonical:
                    raise ValueError(
                        f"city alias {alias!r} belongs to both {owner!r} and "
                        f"{city.canonical!r}"
                    )
            for district in city.districts:
                if district.kato_code in district_codes:
                    raise ValueError(f"duplicate district KATO code {district.kato_code}")
                district_codes.add(district.kato_code)

    def get_city(self, city: str) -> City | None:
        """Return a city by canonical name or any recognized alias."""
        direct = self._by_canonical.get(city)
        if direct is not None:
            return direct
        canonical = self.canonical_city(city)
        return self._by_canonical.get(canonical) if canonical is not None else None

    def canonical_city(self, text: str | None) -> str | None:
        """Resolve a city in free text to the catalog's canonical value."""
        if not text:
            return None
        normalized = normalize_location_text(text)
        for _, pattern, canonical in self._city_matchers:
            if pattern.search(normalized):
                return canonical
        return None

    def canonical_district(self, text: str | None, city: str | None) -> str | None:
        """Resolve a district only within its parent city."""
        if not text or not city:
            return None
        city_record = self.get_city(city)
        if city_record is None:
            return None
        normalized = normalize_location_text(text)
        for _, pattern, canonical in self._district_matchers[city_record.canonical]:
            if pattern.search(normalized):
                return canonical
        return None

    def city_slug(self, city: str) -> str | None:
        """Return the verified Krisha slug, or None when Krisha lacks the city."""
        city_record = self.get_city(city)
        return city_record.krisha_slug if city_record is not None else None

    def districts_for_city(self, city: str) -> tuple[District, ...]:
        """Return official city districts, if the city has any."""
        city_record = self.get_city(city)
        return city_record.districts if city_record is not None else ()

    def cities_for_district(self, text: str) -> tuple[str, ...]:
        """Return every city in which a district label can be resolved."""
        matches = [
            city.canonical
            for city in self.cities
            if self.canonical_district(text, city.canonical) is not None
        ]
        return tuple(matches)

    def find_city_in_text(self, text: str) -> str | None:
        """Alias for request-oriented city lookup."""
        return self.canonical_city(text)

    def find_districts_in_text(
        self,
        text: str,
        city: str | None,
    ) -> tuple[str, ...]:
        """Return all distinct district values mentioned in text."""
        normalized = normalize_location_text(text)
        cities = self.cities if city is None else tuple(
            record for record in (self.get_city(city),) if record is not None
        )
        found: list[str] = []
        for record in cities:
            for _, pattern, canonical in self._district_matchers[record.canonical]:
                if pattern.search(normalized) and canonical not in found:
                    found.append(canonical)
        return tuple(found)

    def unambiguous_district_aliases(self) -> dict[str, str]:
        """Return aliases that identify only one canonical district globally."""
        owners: dict[str, set[str]] = {}
        for city in self.cities:
            for district in city.districts:
                for alias in {
                    district.canonical,
                    district.name_ru,
                    district.name_kk,
                    *district.aliases,
                }:
                    for variant in _alias_variants(alias):
                        owners.setdefault(variant, set()).add(district.canonical)
        return {
            alias: next(iter(canonicals))
            for alias, canonicals in owners.items()
            if len(canonicals) == 1
        }
