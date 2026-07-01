"""Public Kazakhstan location catalog."""

from pathlib import Path

from agent.locations.catalog import (
    CatalogMetadata,
    City,
    District,
    LocationCatalog,
    normalize_location_text,
)
from agent.locations.resolver import (
    LocationInputError,
    ResolvedLocations,
)
from agent.locations.resolver import (
    resolve_locations as _resolve_locations,
)

LOCATIONS = LocationCatalog.from_path(Path(__file__).with_name("kz_locations.json"))


def resolve_locations(
    *,
    message: str,
    default_city: str,
    llm_city: str | None = None,
    llm_districts: list[str] | tuple[str, ...] | None = None,
    existing_city: str | None = None,
    existing_districts: list[str] | tuple[str, ...] | None = None,
) -> ResolvedLocations:
    """Resolve locations with the production Kazakhstan catalog."""
    return _resolve_locations(
        message=message,
        default_city=default_city,
        catalog=LOCATIONS,
        llm_city=llm_city,
        llm_districts=llm_districts,
        existing_city=existing_city,
        existing_districts=existing_districts,
    )


__all__ = [
    "LOCATIONS",
    "CatalogMetadata",
    "City",
    "District",
    "LocationCatalog",
    "LocationInputError",
    "ResolvedLocations",
    "normalize_location_text",
    "resolve_locations",
]
