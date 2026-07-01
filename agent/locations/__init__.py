"""Public Kazakhstan location catalog."""

from pathlib import Path

from agent.locations.catalog import (
    CatalogMetadata,
    City,
    District,
    LocationCatalog,
    normalize_location_text,
)

LOCATIONS = LocationCatalog.from_path(Path(__file__).with_name("kz_locations.json"))

__all__ = [
    "LOCATIONS",
    "CatalogMetadata",
    "City",
    "District",
    "LocationCatalog",
    "normalize_location_text",
]
