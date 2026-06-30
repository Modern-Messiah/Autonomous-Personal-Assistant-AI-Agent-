"""DeepSeek-backed scorer for enriched apartments (OpenAI-compatible API)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.models.score import ApartmentScore

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"


class DeepSeekApartmentScorer:
    """Scores apartments via DeepSeek JSON output over the OpenAI-compatible API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-chat",
        temperature: float = 0.2,
        timeout_seconds: float = 15.0,
        max_retries: int = 1,
        endpoint: str = DEEPSEEK_ENDPOINT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._endpoint = endpoint
        self._transport = transport

    async def score_apartment(
        self,
        apartment: EnrichedApartment,
        criteria: SearchCriteria | None = None,
    ) -> ApartmentScore:
        """Return a structured recommendation for one apartment."""
        payload = self._build_payload(apartment, criteria)
        headers = {"Authorization": f"Bearer {self._api_key}"}

        last_error: Exception | None = None
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            for _ in range(self._max_retries + 1):
                try:
                    response = await client.post(self._endpoint, headers=headers, json=payload)
                    response.raise_for_status()
                    content = self._extract_content(response.json())
                    return ApartmentScore.model_validate(json.loads(content))
                except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                    last_error = exc

        # DeepSeek has no strict JSON schema (only json_object), so retries above
        # absorb the occasional malformed payload before giving up.
        assert last_error is not None
        raise last_error

    def _build_payload(
        self,
        enriched: EnrichedApartment,
        criteria: SearchCriteria | None = None,
    ) -> dict[str, Any]:
        apartment = enriched.apartment
        lines = [
            "Evaluate how well this apartment fits the buyer's search criteria.",
            "Respond with a single JSON object and nothing else.",
            'JSON schema: {"score": number 0-100, '
            '"reasons": array of 2 to 4 short strings, '
            '"recommendation": one of "strong_buy", "consider", "skip"}.',
            "Write each reason in Russian, short and concrete.",
            "Judge fit against the criteria below, not in the abstract: a listing "
            "that matches the budget, rooms and area well should score high even "
            "if nearby-infrastructure counts are 0 (they may just be unavailable).",
        ]
        lines.extend(self._criteria_lines(criteria))
        lines.append("--- listing ---")
        lines.extend(
            [
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
            ]
        )
        user_prompt = "\n".join(lines)
        return {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a real-estate scoring assistant. Output strict JSON only.",
                },
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self._temperature,
            "response_format": {"type": "json_object"},
        }

    @staticmethod
    def _criteria_lines(criteria: SearchCriteria | None) -> list[str]:
        if criteria is None:
            return ["--- buyer criteria ---", "no explicit criteria provided"]
        budget = "any"
        if criteria.min_price_kzt is not None or criteria.max_price_kzt is not None:
            budget = f"{criteria.min_price_kzt or 0} - {criteria.max_price_kzt or 'any'} KZT"
        rooms = ", ".join(str(room) for room in criteria.rooms) if criteria.rooms else "any"
        districts = ", ".join(criteria.districts) if criteria.districts else "any"
        area = "any"
        if criteria.min_area_m2 is not None or criteria.max_area_m2 is not None:
            area = f"{criteria.min_area_m2 or 0} - {criteria.max_area_m2 or 'any'} m2"
        return [
            "--- buyer criteria ---",
            f"deal_type: {criteria.deal_type}",
            f"city: {criteria.city}",
            f"budget_kzt: {budget}",
            f"rooms: {rooms}",
            f"districts: {districts}",
            f"area_m2: {area}",
        ]

    @staticmethod
    def _extract_content(response_data: dict[str, Any]) -> str:
        choices = response_data.get("choices", [])
        if not choices:
            msg = "DeepSeek response did not contain choices"
            raise ValueError(msg)

        message = choices[0].get("message", {})
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            msg = "DeepSeek response did not contain content"
            raise ValueError(msg)

        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.replace("json\n", "", 1).strip()
        return cleaned
