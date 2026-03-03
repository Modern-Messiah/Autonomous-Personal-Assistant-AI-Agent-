"""Scoring model for apartment recommendations."""

from typing import Literal

from pydantic import BaseModel, Field


class ApartmentScore(BaseModel):
    """LLM-generated score and recommendation for one apartment."""

    score: float = Field(ge=0, le=100)
    reasons: list[str]
    recommendation: Literal["strong_buy", "consider", "skip"]

