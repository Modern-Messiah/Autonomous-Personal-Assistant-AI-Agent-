"""Notion database sync client for saved apartments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from agent.models.enriched import EnrichedApartment

NOTION_API_VERSION = "2022-06-28"

_STATUS_ALIASES = ("Status", "Статус")
_PRICE_ALIASES = ("PriceKZT", "Price", "Цена")
_CITY_ALIASES = ("City", "Город")
_DISTRICT_ALIASES = ("District", "Район")
_ADDRESS_ALIASES = ("Address", "Адрес")
_AREA_ALIASES = ("AreaM2", "Area", "Площадь")
_ROOMS_ALIASES = ("Rooms", "Комнаты")
_FLOOR_ALIASES = ("Floor", "Этаж")
_URL_ALIASES = ("KrishaURL", "URL", "Link", "Ссылка")
_EXTERNAL_ID_ALIASES = ("ExternalID", "ListingID", "ID")
_SOURCE_ALIASES = ("Source", "Источник")
_SCORE_ALIASES = ("Score",)
_RECOMMENDATION_ALIASES = ("Recommendation", "Рекомендация")
_PUBLISHED_AT_ALIASES = ("PublishedAt", "Дата публикации")
_SCRAPED_AT_ALIASES = ("ScrapedAt", "\u0414\u0430\u0442\u0430 \u0441\u0431\u043e\u0440\u0430")


@dataclass(slots=True, frozen=True)
class NotionDatabaseSchema:
    """Minimal Notion database schema metadata required for page sync."""

    title_property: str
    properties: dict[str, dict[str, object]]


class NotionClient:
    """Create or update apartment pages in a configured Notion database."""

    def __init__(
        self,
        *,
        api_token: str,
        database_id: str,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._database_id = database_id
        self._timeout_seconds = timeout_seconds
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Notion-Version": NOTION_API_VERSION,
        }
        self._transport = transport
        self._schema: NotionDatabaseSchema | None = None

    async def sync_apartment(
        self,
        apartment: EnrichedApartment,
        *,
        page_id: str | None = None,
    ) -> str:
        """Create or update a Notion page for one apartment."""
        schema = await self._get_schema()
        payload: dict[str, object] = {
            "properties": self._build_properties(apartment, schema),
        }
        if apartment.apartment.photos:
            payload["cover"] = {
                "type": "external",
                "external": {"url": apartment.apartment.photos[0]},
            }

        if page_id is None:
            payload["parent"] = {"database_id": self._database_id}
            payload["children"] = self._build_children(apartment)
            response = await self._request("POST", "/pages", json=payload)
        else:
            response = await self._request("PATCH", f"/pages/{page_id}", json=payload)
        return str(response["id"])

    async def _get_schema(self) -> NotionDatabaseSchema:
        if self._schema is not None:
            return self._schema

        payload = await self._request("GET", f"/databases/{self._database_id}")
        properties = payload.get("properties", {})
        if not isinstance(properties, dict):
            msg = "Notion database response does not contain properties"
            raise ValueError(msg)

        title_property = ""
        normalized_properties: dict[str, dict[str, object]] = {}
        for name, definition in properties.items():
            if not isinstance(definition, dict):
                continue
            normalized_properties[name] = definition
            if definition.get("type") == "title":
                title_property = name

        if not title_property:
            msg = "Notion database must have a title property"
            raise ValueError(msg)

        self._schema = NotionDatabaseSchema(
            title_property=title_property,
            properties=normalized_properties,
        )
        return self._schema

    def _build_properties(
        self,
        item: EnrichedApartment,
        schema: NotionDatabaseSchema,
    ) -> dict[str, object]:
        apartment = item.apartment
        properties: dict[str, object] = {
            schema.title_property: self._encode_title(apartment.title),
        }

        self._set_optional_property(
            properties,
            schema,
            aliases=_PRICE_ALIASES,
            value=apartment.price_kzt,
        )
        self._set_optional_property(
            properties,
            schema,
            aliases=_CITY_ALIASES,
            value=apartment.city,
        )
        self._set_optional_property(
            properties,
            schema,
            aliases=_DISTRICT_ALIASES,
            value=apartment.district,
        )
        self._set_optional_property(
            properties,
            schema,
            aliases=_ADDRESS_ALIASES,
            value=apartment.address,
        )
        self._set_optional_property(
            properties,
            schema,
            aliases=_AREA_ALIASES,
            value=apartment.area_m2,
        )
        self._set_optional_property(
            properties,
            schema,
            aliases=_ROOMS_ALIASES,
            value=apartment.rooms,
        )
        self._set_optional_property(
            properties,
            schema,
            aliases=_FLOOR_ALIASES,
            value=apartment.floor,
        )
        self._set_optional_property(
            properties,
            schema,
            aliases=_URL_ALIASES,
            value=apartment.url,
        )
        self._set_optional_property(
            properties,
            schema,
            aliases=_EXTERNAL_ID_ALIASES,
            value=apartment.external_id,
        )
        self._set_optional_property(
            properties,
            schema,
            aliases=_SOURCE_ALIASES,
            value=apartment.source,
        )
        self._set_optional_property(
            properties,
            schema,
            aliases=_PUBLISHED_AT_ALIASES,
            value=apartment.published_at,
        )
        self._set_optional_property(
            properties,
            schema,
            aliases=_SCRAPED_AT_ALIASES,
            value=apartment.scraped_at,
        )

        if item.score is not None:
            self._set_optional_property(
                properties,
                schema,
                aliases=_SCORE_ALIASES,
                value=item.score.score,
            )
            self._set_optional_property(
                properties,
                schema,
                aliases=_RECOMMENDATION_ALIASES,
                value=item.score.recommendation,
            )

        status_property = self._find_property_name(schema, _STATUS_ALIASES)
        if status_property is not None:
            encoded_status = self._encode_status_or_select(
                schema.properties[status_property],
                preferred_names=("New", "Новая"),
            )
            if encoded_status is not None:
                properties[status_property] = encoded_status

        return properties

    def _set_optional_property(
        self,
        target: dict[str, object],
        schema: NotionDatabaseSchema,
        *,
        aliases: tuple[str, ...],
        value: object,
    ) -> None:
        if value is None:
            return
        property_name = self._find_property_name(schema, aliases)
        if property_name is None:
            return

        encoded = self._encode_value(schema.properties[property_name], value)
        if encoded is not None:
            target[property_name] = encoded

    @staticmethod
    def _find_property_name(
        schema: NotionDatabaseSchema,
        aliases: tuple[str, ...],
    ) -> str | None:
        for alias in aliases:
            if alias in schema.properties:
                return alias
        return None

    @staticmethod
    def _encode_title(value: str) -> dict[str, object]:
        return {
            "title": [
                {
                    "type": "text",
                    "text": {"content": value[:2000]},
                }
            ]
        }

    def _encode_value(
        self,
        definition: dict[str, object],
        value: object,
    ) -> dict[str, object] | None:
        property_type = str(definition.get("type"))
        if property_type == "title" and isinstance(value, str):
            return self._encode_title(value)
        if property_type == "number" and isinstance(value, int | float):
            return {"number": float(value) if isinstance(value, float) else value}
        if property_type == "url" and isinstance(value, str):
            return {"url": value}
        if property_type == "date" and isinstance(value, datetime):
            normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
            return {"date": {"start": normalized.isoformat()}}
        if property_type in {"rich_text", "text"} and isinstance(value, str):
            return {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": value[:2000]},
                    }
                ]
            }
        if property_type == "select" and isinstance(value, str):
            return {"select": {"name": value}}
        if property_type == "status" and isinstance(value, str):
            return {"status": {"name": value}}
        return None

    @staticmethod
    def _encode_status_or_select(
        definition: dict[str, object],
        *,
        preferred_names: tuple[str, ...],
    ) -> dict[str, object] | None:
        property_type = str(definition.get("type"))
        field = definition.get(property_type, {})
        if not isinstance(field, dict):
            return None
        options = field.get("options", [])
        if not isinstance(options, list):
            return None
        option_names = {
            str(option.get("name"))
            for option in options
            if isinstance(option, dict) and option.get("name")
        }
        for preferred_name in preferred_names:
            if preferred_name not in option_names:
                continue
            if property_type == "select":
                return {"select": {"name": preferred_name}}
            if property_type == "status":
                return {"status": {"name": preferred_name}}
        return None

    @staticmethod
    def _build_children(item: EnrichedApartment) -> list[dict[str, object]]:
        apartment = item.apartment
        lines = [
            f"Цена: {apartment.price_kzt:,} KZT".replace(",", " "),
            f"Ссылка: {apartment.url}",
        ]
        if apartment.address is not None:
            lines.append(f"Адрес: {apartment.address}")
        if apartment.area_m2 is not None:
            lines.append(f"Площадь: {apartment.area_m2:g} м2")
        if apartment.floor is not None:
            lines.append(f"Этаж: {apartment.floor}")
        if item.score is not None:
            lines.append(
                f"Score: {item.score.score:.1f} ({item.score.recommendation})"
            )
            if item.score.reasons:
                lines.append("Причины: " + "; ".join(item.score.reasons))
        if item.mortgage_monthly_payment_kzt is not None:
            lines.append(
                "Ипотека: "
                f"{item.mortgage_monthly_payment_kzt:,} KZT/мес, "
                f"переплата {item.mortgage_total_overpayment_kzt or 0:,} KZT".replace(
                    ",",
                    " ",
                )
            )
        if item.nearby_schools is not None or item.nearby_parks is not None:
            lines.append(
                "Рядом: "
                f"школы={item.nearby_schools or 0}, "
                f"парки={item.nearby_parks or 0}, "
                f"метро={item.nearby_metro or 0}"
            )

        return [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": line[:2000]},
                        }
                    ]
                },
            }
            for line in lines
        ]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.notion.com/v1",
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.request(method, path, json=json)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            msg = "Notion API returned non-object JSON payload"
            raise ValueError(msg)
        return payload
