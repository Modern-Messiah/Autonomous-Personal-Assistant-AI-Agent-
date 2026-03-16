"""Tests for Notion sync client."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from agent.models.apartment import Apartment
from agent.models.enriched import EnrichedApartment
from agent.models.score import ApartmentScore
from agent.tools.notion_client import NotionClient


def build_apartment() -> EnrichedApartment:
    return EnrichedApartment(
        apartment=Apartment(
            external_id="555001",
            source="krisha",
            url="https://krisha.kz/a/show/555001",
            title="Notion sync apartment",
            price_kzt=42_000_000,
            city="Almaty",
            district="Medeu",
            address="Dostyk 10",
            area_m2=64.0,
            floor="7/12",
            rooms=3,
            photos=["https://photos.krisha.kz/555001/1.jpg"],
            published_at=datetime(2025, 3, 10, tzinfo=UTC),
            scraped_at=datetime(2025, 3, 11, tzinfo=UTC),
        ),
        score=ApartmentScore(
            score=88.5,
            reasons=["good district", "strong layout"],
            recommendation="strong_buy",
        ),
        nearby_schools=4,
        nearby_parks=2,
        nearby_metro=1,
        mortgage_monthly_payment_kzt=310_000,
        mortgage_total_overpayment_kzt=26_000_000,
    )


def build_database_response() -> dict[str, object]:
    return {
        "object": "database",
        "id": "db-123",
        "properties": {
            "Name": {"id": "title", "type": "title", "title": {}},
            "PriceKZT": {"id": "price", "type": "number", "number": {}},
            "KrishaURL": {"id": "url", "type": "url", "url": {}},
            "Status": {
                "id": "status",
                "type": "select",
                "select": {"options": [{"name": "New"}]},
            },
            "Score": {"id": "score", "type": "number", "number": {}},
            "Recommendation": {
                "id": "recommendation",
                "type": "rich_text",
                "rich_text": {},
            },
        },
    }


@pytest.mark.asyncio
async def test_notion_client_creates_page_with_expected_payload() -> None:
    apartment = build_apartment()
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=build_database_response())

        payload = json.loads(request.content.decode("utf-8"))
        requests.append((request.method, request.url.path, payload))
        return httpx.Response(200, json={"id": "page-created"})

    client = NotionClient(
        api_token="secret-token",
        database_id="db-123",
        transport=httpx.MockTransport(handler),
    )

    page_id = await client.sync_apartment(apartment)

    assert page_id == "page-created"
    assert len(requests) == 1
    method, path, payload = requests[0]
    assert method == "POST"
    assert path == "/v1/pages"
    assert payload["parent"] == {"database_id": "db-123"}
    assert payload["cover"] == {
        "type": "external",
        "external": {"url": "https://photos.krisha.kz/555001/1.jpg"},
    }
    properties = payload["properties"]
    assert properties["Name"]["title"][0]["text"]["content"] == "Notion sync apartment"
    assert properties["PriceKZT"] == {"number": 42_000_000}
    assert properties["KrishaURL"] == {"url": "https://krisha.kz/a/show/555001"}
    assert properties["Status"] == {"select": {"name": "New"}}
    assert properties["Score"] == {"number": 88.5}
    assert "children" in payload


@pytest.mark.asyncio
async def test_notion_client_updates_existing_page_without_children() -> None:
    apartment = build_apartment()
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=build_database_response())

        payload = json.loads(request.content.decode("utf-8"))
        requests.append((request.method, request.url.path, payload))
        return httpx.Response(200, json={"id": "page-existing"})

    client = NotionClient(
        api_token="secret-token",
        database_id="db-123",
        transport=httpx.MockTransport(handler),
    )

    page_id = await client.sync_apartment(apartment, page_id="page-existing")

    assert page_id == "page-existing"
    assert len(requests) == 1
    method, path, payload = requests[0]
    assert method == "PATCH"
    assert path == "/v1/pages/page-existing"
    assert "parent" not in payload
    assert "children" not in payload
