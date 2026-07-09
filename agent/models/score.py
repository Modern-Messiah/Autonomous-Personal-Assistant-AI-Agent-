"""Scoring model for apartment recommendations."""

from typing import Literal

from pydantic import BaseModel, Field


class ApartmentScore(BaseModel):
    """LLM-generated score and recommendation for one apartment."""

    score: float = Field(ge=0, le=100)
    reasons: list[str]
    recommendation: Literal["strong_buy", "consider", "skip"]
    # 1-2 sentence digest of the raw «описание»: the concrete essentials (ЖК,
    # срок сдачи, отделка, мебель, торг) without realtor marketing fluff. The
    # card shows this instead of a truncated raw description when present.
    description_summary: str | None = None

