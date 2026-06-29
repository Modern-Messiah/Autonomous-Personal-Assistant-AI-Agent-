"""Tests for the DeepSeek-backed LLM intent parser."""

from __future__ import annotations

import json

import httpx
import pytest

from agent.tools.llm_intent_parser import LLMIntentParser


@pytest.mark.asyncio
async def test_llm_intent_parser_posts_openai_compatible_request() -> None:
    expected = {
        "city": "Karaganda",
        "deal_type": "sale",
        "max_price_kzt": 30_000_000,
        "rooms": [2],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "deepseek.com" in str(request.url)
        assert request.headers["authorization"] == "Bearer test-key"

        payload = json.loads(request.content.decode("utf-8"))
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"
        return httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": json.dumps(expected)}}]},
        )

    parser = LLMIntentParser(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )

    result = await parser.parse_patch(
        message="двухкомнатная в Караганде до 30 млн",
    )

    assert result == expected
