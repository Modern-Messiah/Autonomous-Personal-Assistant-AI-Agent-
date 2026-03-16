"""Gemini-backed scorer for enriched apartments."""

from __future__ import annotations

import json
from typing import Any

import httpx

from agent.models.enriched import EnrichedApartment
from agent.models.score import ApartmentScore


class GeminiApartmentScorer:
    """Scores apartments via Gemini structured JSON output."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.5-flash",
        temperature: float = 0.2,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    async def score_apartment(self, apartment: EnrichedApartment) -> ApartmentScore:
        """Return a structured recommendation for one apartment."""
        payload = self._build_payload(apartment)
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(
                self._endpoint,
                params={"key": self._api_key},
                json=payload,
            )
            response.raise_for_status()

        response_data = response.json()
        score_json = self._extract_json_text(response_data)
        parsed = json.loads(score_json)
        return ApartmentScore.model_validate(parsed)

    def _build_payload(self, enriched: EnrichedApartment) -> dict[str, Any]:
        apartment = enriched.apartment
        prompt = "\n".join(
            [
                "Evaluate this apartment for a real-estate search assistant.",
                "Return JSON only.",
                f"title: {apartment.title}",
                f"price_kzt: {apartment.price_kzt}",
                f"city: {apartment.city}",
                f"district: {apartment.district or 'unknown'}",
                f"address: {apartment.address or 'unknown'}",
                f"area_m2: {apartment.area_m2 or 'unknown'}",
                f"floor: {apartment.floor or 'unknown'}",
                f"rooms: {apartment.rooms or 'unknown'}",
                f"nearby_schools: {enriched.nearby_schools or 0}",
                f"nearby_parks: {enriched.nearby_parks or 0}",
                f"nearby_metro: {enriched.nearby_metro or 0}",
                (
                    "mortgage_monthly_payment_kzt: "
                    f"{enriched.mortgage_monthly_payment_kzt or 'unknown'}"
                ),
                (
                    "mortgage_total_overpayment_kzt: "
                    f"{enriched.mortgage_total_overpayment_kzt or 'unknown'}"
                ),
                "score must be from 0 to 100",
                "recommendation must be one of: strong_buy, consider, skip",
                "reasons must contain 2 to 4 concise strings",
            ]
        )
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": self._temperature,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "required": ["score", "reasons", "recommendation"],
                    "properties": {
                        "score": {"type": "NUMBER"},
                        "reasons": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                        },
                        "recommendation": {
                            "type": "STRING",
                            "enum": ["strong_buy", "consider", "skip"],
                        },
                    },
                },
            },
        }

    @staticmethod
    def _extract_json_text(response_data: dict[str, Any]) -> str:
        candidates = response_data.get("candidates", [])
        if not candidates:
            msg = "Gemini response did not contain candidates"
            raise ValueError(msg)

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            msg = "Gemini response candidate did not contain parts"
            raise ValueError(msg)

        text = parts[0].get("text")
        if not isinstance(text, str) or not text.strip():
            msg = "Gemini response did not contain JSON text"
            raise ValueError(msg)

        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.replace("json\n", "", 1).strip()
        return cleaned

