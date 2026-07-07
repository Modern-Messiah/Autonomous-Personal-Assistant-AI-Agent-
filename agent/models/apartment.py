"""Apartment model returned by parsers."""

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class Apartment(BaseModel):
    """Raw apartment listing from an external source."""

    external_id: str = Field(min_length=1)
    source: Literal["krisha"] = "krisha"
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    price_kzt: int = Field(gt=0)
    city: str = Field(min_length=1)
    district: str | None = None
    address: str | None = None
    area_m2: float | None = Field(default=None, gt=0)
    floor: str | None = None
    rooms: int | None = Field(default=None, gt=0)
    # Who posted the listing — the owner ("Хозяин недвижимости"), an agent/agency
    # (company block), or a developer (builder block on new-construction ads);
    # None when the detail page didn't say.
    posted_by: Literal["owner", "agent", "developer"] | None = None
    # Agency name when posted_by == "agent" (e.g. "Top City").
    agency_name: str | None = None
    # Free-text «Описание» — the richest condition/layout/extras signal for the LLM.
    description: str | None = None
    # krisha's own «на X% дешевле/дороже рынка города» verdict (signed: negative =
    # cheaper than the city market for similar flats). None when it has no widget.
    market_diff_percent: float | None = None
    # about-flat params.
    build_year: int | None = Field(default=None, ge=1900, le=2100)
    building_type: str | None = None  # монолитный / панельный / кирпичный / …
    ceiling_height_m: float | None = Field(default=None, gt=0)
    furnished: str | None = None  # «Квартира меблирована»: да / частично / нет
    photos: list[str]
    published_at: datetime | None = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            msg = "url must be a valid absolute http(s) URL"
            raise ValueError(msg)
        return value

    @field_validator("photos")
    @classmethod
    def validate_photos(cls, value: list[str]) -> list[str]:
        for photo in value:
            parsed = urlparse(photo)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                msg = "each photo must be a valid absolute http(s) URL"
                raise ValueError(msg)
        return value
