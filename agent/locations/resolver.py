"""Resolve free-form request locations against the trusted catalog."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from agent.locations.catalog import LocationCatalog


@dataclass(frozen=True, slots=True)
class ResolvedLocations:
    """Canonical location values plus defaulting metadata."""

    city: str
    districts: tuple[str, ...] | None
    defaulted_city: bool


class LocationInputError(ValueError):
    """Expected user-facing location validation failure."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _district_matches(
    catalog: LocationCatalog,
    text: str,
) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for city in catalog.cities:
        canonical = catalog.canonical_district(text, city.canonical)
        if canonical is not None:
            matches.append((city.canonical, canonical))
    return matches


def _valid_district_names(catalog: LocationCatalog, city: str) -> str:
    names = [district.name_ru for district in catalog.districts_for_city(city)]
    return ", ".join(names)


def _resolve_explicit_districts(
    *,
    catalog: LocationCatalog,
    raw_districts: Iterable[str],
    city: str | None,
) -> tuple[str, tuple[str, ...]]:
    resolved_city = city
    resolved: list[str] = []

    for raw in raw_districts:
        if resolved_city is not None:
            canonical = catalog.canonical_district(raw, resolved_city)
            if canonical is None:
                other_cities = {
                    candidate_city for candidate_city, _ in _district_matches(catalog, raw)
                }
                if other_cities:
                    msg = f"Район «{raw}» не относится к городу {resolved_city}."
                else:
                    valid = _valid_district_names(catalog, resolved_city)
                    suffix = f" Доступные районы: {valid}." if valid else ""
                    msg = f"Район «{raw}» не удалось распознать.{suffix}"
                raise LocationInputError(msg)
        else:
            matches = _district_matches(catalog, raw)
            candidate_cities = {candidate_city for candidate_city, _ in matches}
            if not matches:
                raise LocationInputError(f"Район «{raw}» не удалось распознать.")
            if len(candidate_cities) > 1:
                raise LocationInputError(
                    f"Район «{raw}» встречается в нескольких городах — укажи город."
                )
            resolved_city = matches[0][0]
            canonical = matches[0][1]

        if canonical not in resolved:
            resolved.append(canonical)

    if resolved_city is None:
        raise LocationInputError(
            "Не удалось определить город для выбранного района."  # noqa: RUF001
        )
    return resolved_city, tuple(resolved)


def resolve_locations(
    *,
    message: str,
    default_city: str,
    catalog: LocationCatalog,
    llm_city: str | None = None,
    llm_districts: Iterable[str] | None = None,
    existing_city: str | None = None,
    existing_districts: Iterable[str] | None = None,
) -> ResolvedLocations:
    """Resolve new-search or refinement locations with strict validation."""
    explicit_city = None
    if llm_city is not None:
        explicit_city = catalog.canonical_city(llm_city)
        if explicit_city is None:
            raise LocationInputError(f"Город «{llm_city}» не удалось распознать.")

    message_city = catalog.find_city_in_text(message)
    selected_city = explicit_city or message_city
    city_was_supplied = selected_city is not None

    raw_districts = tuple(llm_districts or ())
    selected_districts: tuple[str, ...] | None = None
    if raw_districts:
        selected_city, selected_districts = _resolve_explicit_districts(
            catalog=catalog,
            raw_districts=raw_districts,
            city=selected_city,
        )
        city_was_supplied = True
    elif selected_city is not None:
        found = catalog.find_districts_in_text(message, selected_city)
        selected_districts = found or None
    elif existing_city is not None:
        found = catalog.find_districts_in_text(message, existing_city)
        if found:
            selected_city = existing_city
            selected_districts = found
            city_was_supplied = True
    elif existing_city is None:
        matches = _district_matches(catalog, message)
        candidate_cities = {candidate_city for candidate_city, _ in matches}
        if len(candidate_cities) == 1:
            selected_city = matches[0][0]
            selected_districts = tuple(dict.fromkeys(match[1] for match in matches))
            city_was_supplied = True
        elif len(candidate_cities) > 1:
            raise LocationInputError(
                "Название района встречается в нескольких городах — укажи город."
            )

    if selected_city is None:
        selected_city = existing_city or catalog.canonical_city(default_city)
    if selected_city is None:
        raise ValueError(f"default city {default_city!r} is absent from location catalog")

    if (existing_city is not None and not city_was_supplied) or (
        existing_city is not None
        and selected_city == existing_city
        and selected_districts is None
        and not raw_districts
    ):
        selected_districts = (
            tuple(existing_districts) if existing_districts is not None else None
        )

    if catalog.city_slug(selected_city) is None:
        city_record = catalog.get_city(selected_city)
        display_name = city_record.name_ru if city_record is not None else selected_city
        raise LocationInputError(
            f"Город {display_name} распознан, но сейчас отсутствует в каталоге Krisha."
        )

    return ResolvedLocations(
        city=selected_city,
        districts=selected_districts,
        defaulted_city=not city_was_supplied and existing_city is None,
    )
