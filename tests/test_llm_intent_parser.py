"""Tests for the DeepSeek-backed LLM intent parser."""

from __future__ import annotations

import json

import httpx
import pytest

from agent.tools.llm_intent_parser import LLMIntentParser


@pytest.mark.asyncio
async def test_llm_intent_parser_posts_openai_compatible_request() -> None:
    expected = {
        "city": "Қарағанды",
        "districts": ["Қазыбек би ауданы"],
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
        prompt = payload["messages"][1]["content"]
        assert "Return city and district names as written by the user" in prompt
        assert "suitable for Krisha paths" not in prompt
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


@pytest.mark.asyncio
async def test_llm_intent_parser_retries_transient_5xx_with_backoff() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status_code=500, text="upstream error")
        return httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": json.dumps({"rooms": [2]})}}]},
        )

    parser = LLMIntentParser(api_key="test-key", transport=httpx.MockTransport(handler))

    # previously the loop hammered the API without delay; now a transient 5xx is
    # retried (with backoff inside request_with_retry) and the parse succeeds
    result = await parser.parse_patch(message="2 комнаты")

    assert result == {"rooms": [2]}
    assert calls == 2


@pytest.mark.asyncio
async def test_llm_intent_parser_fails_fast_on_client_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code=401, text="bad key")

    parser = LLMIntentParser(api_key="bad-key", transport=httpx.MockTransport(handler))

    # a non-429 4xx (bad key) is not transient: one request, immediate raise
    with pytest.raises(httpx.HTTPStatusError):
        await parser.parse_patch(message="2 комнаты")

    assert calls == 1


@pytest.mark.asyncio
async def test_llm_intent_parser_resamples_malformed_llm_output() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = "not json at all" if calls == 1 else json.dumps({"city": "Алматы"})
        return httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": content}}]},
        )

    parser = LLMIntentParser(api_key="test-key", transport=httpx.MockTransport(handler))

    # malformed LLM output is re-sent (fresh sample), not raised on first try
    result = await parser.parse_patch(message="в Алматы")

    assert result == {"city": "Алматы"}
    assert calls == 2
