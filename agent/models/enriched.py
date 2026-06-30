"""Enriched apartment model."""

from pydantic import BaseModel, Field

from agent.models.apartment import Apartment
from agent.models.score import ApartmentScore


class EnrichedApartment(BaseModel):
    """Apartment with area metadata and optional scoring output."""

    apartment: Apartment
    score: ApartmentScore | None = None
    nearby_schools: int | None = Field(default=None, ge=0)
    nearby_parks: int | None = Field(default=None, ge=0)
    nearby_metro: int | None = Field(default=None, ge=0)
    mortgage_monthly_payment_kzt: int | None = Field(default=None, ge=0)
    mortgage_total_overpayment_kzt: int | None = Field(default=None, ge=0)

