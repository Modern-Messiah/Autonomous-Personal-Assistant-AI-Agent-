"""Apartment model returned by parsers."""

from datetime import datetime, timezone
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
    photos: list[str]
    published_at: datetime | None = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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
