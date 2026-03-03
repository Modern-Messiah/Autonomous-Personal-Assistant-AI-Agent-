"""Search criteria model."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class SearchCriteria(BaseModel):
    """Criteria used by the parser and ranking pipeline."""

    user_id: int = Field(gt=0)
    city: str = Field(min_length=1)
    deal_type: Literal["sale", "rent"]
    property_type: Literal["apartment"] = "apartment"
    min_price_kzt: int | None = Field(default=None, ge=0)
    max_price_kzt: int | None = Field(default=None, ge=0)
    rooms: list[int] | None = None
    districts: list[str] | None = None
    min_area_m2: float | None = Field(default=None, ge=0)
    max_area_m2: float | None = Field(default=None, ge=0)
    page_limit: int = Field(default=3, ge=1, le=20)

    @field_validator("rooms")
    @classmethod
    def validate_rooms(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        if not value:
            msg = "rooms cannot be empty when provided"
            raise ValueError(msg)
        if any(room <= 0 for room in value):
            msg = "rooms values must be positive"
            raise ValueError(msg)
        return sorted(set(value))

    @field_validator("districts")
    @classmethod
    def validate_districts(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        cleaned = [district.strip() for district in value if district.strip()]
        if not cleaned:
            msg = "districts cannot be empty when provided"
            raise ValueError(msg)
        return cleaned

    @field_validator("city")
    @classmethod
    def normalize_city(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "city cannot be blank"
            raise ValueError(msg)
        return normalized

    @model_validator(mode="after")
    def validate_ranges(self) -> "SearchCriteria":
        if (
            self.min_price_kzt is not None
            and self.max_price_kzt is not None
            and self.min_price_kzt > self.max_price_kzt
        ):
            msg = "min_price_kzt cannot be greater than max_price_kzt"
            raise ValueError(msg)
        if (
            self.min_area_m2 is not None
            and self.max_area_m2 is not None
            and self.min_area_m2 > self.max_area_m2
        ):
            msg = "min_area_m2 cannot be greater than max_area_m2"
            raise ValueError(msg)
        return self

