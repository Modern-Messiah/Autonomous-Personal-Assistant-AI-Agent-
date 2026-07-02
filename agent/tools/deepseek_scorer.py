"""DeepSeek-backed scorer for enriched apartments (OpenAI-compatible API)."""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.models.score import ApartmentScore

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"


def _or_unknown(value: int | None) -> int | str:
    """Keep a real 0 (truly none) but report missing data as 'unknown'."""
    return value if value is not None else "unknown"


def _nearest(distance_m: int | None) -> str:
    """Append the distance to the nearest object, e.g. ' (nearest 480m)'."""
    return f" (nearest {distance_m}m)" if distance_m is not None else ""


class DeepSeekApartmentScorer:
    """Scores a whole shortlist in one call so scores are comparative, not uniform."""

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

    async def score_apartments(
        self,
        apartments: list[EnrichedApartment],
        criteria: SearchCriteria | None = None,
    ) -> list[ApartmentScore | None]:
        """Score all apartments together; returns one score per item (None on failure)."""
        if not apartments:
            return []

        payload = self._build_payload(apartments, criteria)
        headers = {"Authorization": f"Bearer {self._api_key}"}

        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await client.post(self._endpoint, headers=headers, json=payload)
                    response.raise_for_status()
                    content = self._extract_content(response.json())
                    return self._parse_scores(content, count=len(apartments))
                except (httpx.HTTPError, json.JSONDecodeError, ValueError):
                    if attempt < self._max_retries:
                        # Backoff + jitter so a rate-limit/transient error isn't hit
                        # again immediately on retry.
                        await asyncio.sleep(min(4.0, 0.5 * (2**attempt)) + random.uniform(0, 0.5))
                    continue

        # DeepSeek has no strict JSON schema, so on persistent failure degrade
        # gracefully: the pipeline keeps the listings, just without scores.
        return [None] * len(apartments)

    def _build_payload(
        self,
        apartments: list[EnrichedApartment],
        criteria: SearchCriteria | None,
    ) -> dict[str, Any]:
        lines = [
            "You rank apartments that ALL already match the buyer's hard filters "
            "(budget, rooms, area). Compare them against each other.",
            "Score each on overall value/quality from 0 to 100 and DIFFERENTIATE: "
            "use the full range, the best clearly higher than the weakest, and do "
            "not give several listings the same score.",
            "Judge each listing on these factors: (1) price per m², (2) floor "
            "(mid is best, 1st/last worst), (3) area for the price, (4) district, "
            "and (5) LOCATION QUALITY — walking proximity to metro/schools/parks.",
            "Location quality is a first-class factor, weigh it like price/floor: "
            "'nearest Nm' is the distance to the closest such object, and a CLOSER "
            "metro/school/park is clearly better than a far one even at the same "
            "count (e.g. metro 300m beats metro 1500m; 'metro 1 (nearest 350m)' is "
            "a strong plus). Weigh distance, not just the count.",
            "Penalize 1st or last floor, high price per m², a far or absent metro, "
            "few amenities nearby, a cramped area.",
            "A nearby count of 'unknown' means the data is unavailable (e.g. a city "
            "with no metro) — treat it neutrally, do NOT penalize it as if nothing "
            "is nearby (0 means truly none).",
            "recommendation must be one of strong_buy, consider, skip and stay "
            "consistent with the score.",
            "Write 2-4 short reasons per listing in Russian, naming the concrete "
            "differentiators including distances (e.g. «метро 350 м», «дешевле "
            "за м²», «школа рядом 200 м», «высокий этаж»).",
            'Respond with one JSON object: {"items": [{"index": <listing number>, '
            '"score": <0-100>, "recommendation": "strong_buy"|"consider"|"skip", '
            '"reasons": ["..."]}]}. Include every listing exactly once.',
        ]
        lines.extend(self._criteria_lines(criteria))
        lines.append("--- listings ---")
        for index, enriched in enumerate(apartments, start=1):
            lines.append(self._listing_line(index, enriched))

        return {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a real-estate scoring assistant. Output strict JSON only.",
                },
                {"role": "user", "content": "\n".join(lines)},
            ],
            "temperature": self._temperature,
            "response_format": {"type": "json_object"},
        }

    @staticmethod
    def _listing_line(index: int, enriched: EnrichedApartment) -> str:
        apartment = enriched.apartment
        price_per_m2 = "unknown"
        if apartment.area_m2 and apartment.area_m2 > 0:
            price_per_m2 = str(round(apartment.price_kzt / apartment.area_m2))
        return (
            f"[{index}] price_kzt={apartment.price_kzt}, price_per_m2={price_per_m2}, "
            f"rooms={apartment.rooms or 'unknown'}, area_m2={apartment.area_m2 or 'unknown'}, "
            f"floor={apartment.floor or 'unknown'}, "
            f"district={apartment.district or 'unknown'}, "
            f"schools={_or_unknown(enriched.nearby_schools)}{_nearest(enriched.nearby_school_m)}, "
            f"parks={_or_unknown(enriched.nearby_parks)}{_nearest(enriched.nearby_park_m)}, "
            f"metro={_or_unknown(enriched.nearby_metro)}{_nearest(enriched.nearby_metro_m)}, "
            f"mortgage_monthly_kzt={enriched.mortgage_monthly_payment_kzt or 'unknown'}"
        )

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
    def _parse_scores(content: str, *, count: int) -> list[ApartmentScore | None]:
        data = json.loads(content)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            msg = "DeepSeek batch response did not contain an items list"
            raise ValueError(msg)

        scores: list[ApartmentScore | None] = [None] * count
        for entry in items:
            if not isinstance(entry, dict):
                continue
            index = entry.get("index")
            if not isinstance(index, int) or not (1 <= index <= count):
                continue
            try:
                scores[index - 1] = ApartmentScore.model_validate(
                    {
                        "score": entry.get("score"),
                        "reasons": entry.get("reasons"),
                        "recommendation": entry.get("recommendation"),
                    }
                )
            except Exception:
                continue
        return scores

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
