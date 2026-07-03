"""DeepSeek-backed parser for extracting apartment search criteria patches."""

from __future__ import annotations

import json
from typing import Any

import httpx

from agent.models.criteria import SearchCriteria

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"


class LLMIntentParser:
    """Extract JSON search-criteria patches from free-form user messages."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-chat",
        temperature: float = 0.0,
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

    async def parse_patch(
        self,
        *,
        message: str,
        existing_criteria: SearchCriteria | None = None,
    ) -> dict[str, object]:
        """Return a JSON-like patch with only the criteria the user expressed."""
        payload = self._build_payload(
            message=message,
            existing_criteria=existing_criteria,
        )
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
                    parsed = json.loads(content)
                    if not isinstance(parsed, dict):
                        msg = "LLM intent parser expected a JSON object"
                        raise ValueError(msg)
                    return parsed
                except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                    last_error = exc

        assert last_error is not None
        raise last_error

    def _build_payload(
        self,
        *,
        message: str,
        existing_criteria: SearchCriteria | None,
    ) -> dict[str, Any]:
        current_criteria = (
            "null"
            if existing_criteria is None
            else json.dumps(existing_criteria.model_dump(mode="json"), ensure_ascii=False)
        )
        mode = "new_search" if existing_criteria is None else "refine_existing_search"
        user_prompt = "\n".join(
            [
                "Extract apartment search criteria from the user's message.",
                "Return one JSON object and nothing else.",
                (
                    'Use exactly these keys: {"city": string|null, '
                    '"deal_type": "sale"|"rent"|null, '
                    '"rent_period": "monthly"|"daily"|"hourly"|null, '
                    '"min_price_kzt": integer|null, "max_price_kzt": integer|null, '
                    '"rooms": array<integer>|null, "districts": array<string>|null, '
                    '"min_area_m2": number|null, "max_area_m2": number|null, '
                    '"owner_only": boolean|null, '
                    '"page_limit": integer|null}.'
                ),
                (
                    "rent_period only for rent: «помесячно»=monthly, "
                    "«посуточно»=daily, «по часам»=hourly; null when not stated."
                ),
                (
                    "owner_only=true only when the user asks for owner-posted "
                    "listings («от хозяина», «от собственника», «без риелторов»); "
                    "otherwise null."
                ),
                "Property type is always apartment, so do not include property_type.",
                "Use integer prices in KZT and numeric areas in square meters.",
                'Normalize deal_type to "sale" or "rent".',
                (
                    "Return city and district names as written by the user, with "
                    "surrounding punctuation removed. Do not translate, transliterate, "
                    "guess, or replace a location. Deterministic application code "
                    "validates locations after extraction."
                ),
                (
                    'Interpret room phrases like "двухкомнатная" as [2] and '
                    '"2-3 комнаты" as [2, 3].'
                ),
                "If a field is not stated or not reliable, return null for that field.",
                (
                    "If current criteria are provided, only return fields that are explicitly "
                    "changed or newly clarified by the new message. Leave untouched fields null."
                ),
                f"Mode: {mode}",
                f"Current criteria: {current_criteria}",
                f"User message: {message}",
            ]
        )
        return {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You extract structured apartment-search filters. "
                        "Output strict JSON only."
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self._temperature,
            "response_format": {"type": "json_object"},
        }

    @staticmethod
    def _extract_content(response_data: dict[str, Any]) -> str:
        choices = response_data.get("choices", [])
        if not choices:
            msg = "LLM intent parser response did not contain choices"
            raise ValueError(msg)

        message = choices[0].get("message", {})
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            msg = "LLM intent parser response did not contain content"
            raise ValueError(msg)

        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.replace("json\n", "", 1).strip()
        return cleaned
