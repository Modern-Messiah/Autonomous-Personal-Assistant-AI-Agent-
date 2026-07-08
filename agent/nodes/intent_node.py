"""Intent node that converts user text into SearchCriteria."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.locations import ResolvedLocations, resolve_locations
from agent.models.criteria import SearchCriteria
from agent.tools.llm_intent_parser import LLMIntentParser
from agent.tools.regex_intent_parser import RegexIntentParser
from config.settings import Settings, get_settings


class LLMIntentParserProtocol(Protocol):
    """Contract for optional LLM-backed criteria extraction."""

    async def parse_patch(
        self,
        *,
        message: str,
        existing_criteria: SearchCriteria | None = None,
    ) -> dict[str, object]: ...


class IntentCriteriaPatch(BaseModel):
    """Validated partial criteria extracted by the LLM parser."""

    model_config = ConfigDict(extra="ignore")

    city: str | None = None
    deal_type: Literal["sale", "rent"] | None = None
    rent_period: Literal["monthly", "daily", "hourly"] | None = None
    min_price_kzt: int | None = Field(default=None, ge=0)
    max_price_kzt: int | None = Field(default=None, ge=0)
    rooms: list[int] | None = None
    districts: list[str] | None = None
    min_area_m2: float | None = Field(default=None, ge=0)
    max_area_m2: float | None = Field(default=None, ge=0)
    owner_only: bool | None = None
    page_limit: int | None = Field(default=None, ge=1, le=20)

    @field_validator("city", mode="before")
    @classmethod
    def normalize_city_input(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None

    @field_validator("deal_type", mode="before")
    @classmethod
    def normalize_deal_type(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        lowered = value.strip().lower()
        if lowered in {"sale", "buy", "покупка", "купить", "продажа"}:
            return "sale"
        if lowered in {"rent", "аренда", "снять"}:
            return "rent"
        return value

    @field_validator("rooms", mode="before")
    @classmethod
    def normalize_rooms(cls, value: object) -> list[int] | None:
        if value is None or value == "":
            return None

        candidates: list[int] = []
        if isinstance(value, int):
            candidates = [value]
        elif isinstance(value, float):
            candidates = [int(value)]
        elif isinstance(value, str):
            normalized = value.strip().lower()
            range_match = re.fullmatch(r"(\d+)\s*(?:-|\u2013|to)\s*(\d+)", normalized)
            if range_match is not None:
                start = int(range_match.group(1))
                end = int(range_match.group(2))
                if start <= end:
                    candidates = list(range(start, end + 1))
            else:
                candidates = [int(item) for item in re.findall(r"\d+", normalized)]
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, int):
                    candidates.append(item)
                elif isinstance(item, float):
                    candidates.append(int(item))
                elif isinstance(item, str):
                    candidates.extend(int(found) for found in re.findall(r"\d+", item))

        cleaned = sorted({room for room in candidates if room > 0})
        return cleaned or None

    @field_validator("districts", mode="before")
    @classmethod
    def normalize_districts_input(cls, value: object) -> list[str] | None:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return None

        cleaned = [str(item).strip() for item in value if str(item).strip()]
        return cleaned or None

    @field_validator("page_limit", mode="before")
    @classmethod
    def normalize_page_limit(cls, value: object) -> object:
        if isinstance(value, str) and value.strip():
            return int(float(value.strip()))
        if isinstance(value, float):
            return int(value)
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> IntentCriteriaPatch:
        if (
            self.min_price_kzt is not None
            and self.max_price_kzt is not None
            and self.min_price_kzt > self.max_price_kzt
        ):
            self.min_price_kzt, self.max_price_kzt = (
                self.max_price_kzt,
                self.min_price_kzt,
            )
        if (
            self.min_area_m2 is not None
            and self.max_area_m2 is not None
            and self.min_area_m2 > self.max_area_m2
        ):
            self.min_area_m2, self.max_area_m2 = (
                self.max_area_m2,
                self.min_area_m2,
            )
        if self.page_limit is not None:
            self.page_limit = min(max(self.page_limit, 1), 20)
        return self

    def has_values(self) -> bool:
        """Return True when at least one field is meaningfully set."""
        return any(
            getattr(self, field_name) is not None
            for field_name in (
                "city",
                "deal_type",
                "rent_period",
                "min_price_kzt",
                "max_price_kzt",
                "rooms",
                "districts",
                "min_area_m2",
                "max_area_m2",
                "owner_only",
                "page_limit",
            )
        )


class IntentState(TypedDict, total=False):
    """State used by intent node."""

    user_id: int
    message: str
    criteria: SearchCriteria


@dataclass(slots=True, frozen=True)
class ParsedIntent:
    """Search criteria plus user-visible defaulting metadata."""

    criteria: SearchCriteria
    defaulted_city: bool = False


def create_default_llm_intent_parser(
    *, settings: Settings | None = None
) -> LLMIntentParserProtocol | None:
    """Create the production LLM parser from settings, or disable it safely."""
    try:
        settings = settings or get_settings()
        api_key = settings.api.deepseek_api_key.get_secret_value()
    except Exception:
        return None

    if not api_key:
        return None

    try:
        return LLMIntentParser(
            api_key=api_key,
            model=settings.scoring.model,
            timeout_seconds=settings.scoring.timeout_seconds,
        )
    except Exception:
        return None


class IntentNode:
    """LLM-assisted parser with regex fallback for search criteria extraction."""

    def __init__(
        self,
        *,
        default_city: str = "Almaty",
        default_deal_type: Literal["sale", "rent"] = "sale",
        default_page_limit: int = 3,
        llm_parser: LLMIntentParserProtocol | None = None,
        llm_parser_factory: Callable[[], LLMIntentParserProtocol | None] | None = None,
    ) -> None:
        self._default_city = default_city
        self._default_deal_type = default_deal_type
        self._default_page_limit = default_page_limit
        self._llm_parser = llm_parser
        self._llm_parser_factory = llm_parser_factory or create_default_llm_intent_parser
        self._llm_parser_resolved = llm_parser is not None
        self._regex = RegexIntentParser(
            default_deal_type=default_deal_type,
            default_page_limit=default_page_limit,
        )

    async def __call__(self, state: IntentState) -> IntentState:
        criteria = await self.parse(user_id=state["user_id"], message=state["message"])
        return {
            "user_id": state["user_id"],
            "message": state["message"],
            "criteria": criteria,
        }

    async def parse(self, *, user_id: int, message: str) -> SearchCriteria:
        """Parse free-form message into SearchCriteria."""
        return (await self.parse_with_metadata(user_id=user_id, message=message)).criteria

    async def parse_with_metadata(self, *, user_id: int, message: str) -> ParsedIntent:
        """Parse criteria and report when the configured default city was used."""
        patch = await self._parse_with_llm(message=message, existing_criteria=None)
        if patch is not None:
            # Regex backstops so «от хозяина» / «посуточно» work even if the LLM
            # missed them.
            if patch.owner_only is None:
                patch = patch.model_copy(
                    update={"owner_only": self._regex.find_owner_only(message)}
                )
            if patch.rent_period is None:
                patch = patch.model_copy(
                    update={"rent_period": self._regex.find_rent_period(message)}
                )
            locations = resolve_locations(
                message=message,
                default_city=self._default_city,
                llm_city=patch.city,
                llm_districts=patch.districts,
            )
            return ParsedIntent(
                criteria=self._build_search_criteria(
                    user_id=user_id,
                    patch=patch,
                    locations=locations,
                ),
                defaulted_city=locations.defaulted_city,
            )
        locations = resolve_locations(
            message=message,
            default_city=self._default_city,
        )
        return ParsedIntent(
            criteria=self._regex.parse(
                user_id=user_id,
                message=message,
                locations=locations,
            ),
            defaulted_city=locations.defaulted_city,
        )

    async def refine(self, *, criteria: SearchCriteria, message: str) -> SearchCriteria:
        """Merge free-form refinement text into existing criteria."""
        patch = await self._parse_with_llm(message=message, existing_criteria=criteria)
        if patch is not None:
            if patch.owner_only is None:
                patch = patch.model_copy(
                    update={"owner_only": self._regex.find_owner_only(message)}
                )
            if patch.rent_period is None:
                patch = patch.model_copy(
                    update={"rent_period": self._regex.find_rent_period(message)}
                )
            locations = resolve_locations(
                message=message,
                default_city=self._default_city,
                llm_city=patch.city,
                llm_districts=patch.districts,
                existing_city=criteria.city,
                existing_districts=criteria.districts,
            )
            return self._build_refined_criteria(
                criteria=criteria,
                patch=patch,
                locations=locations,
            )
        locations = resolve_locations(
            message=message,
            default_city=self._default_city,
            existing_city=criteria.city,
            existing_districts=criteria.districts,
        )
        return self._regex.refine(
            criteria=criteria,
            message=message,
            locations=locations,
        )

    async def _parse_with_llm(
        self,
        *,
        message: str,
        existing_criteria: SearchCriteria | None,
    ) -> IntentCriteriaPatch | None:
        parser = self._resolve_llm_parser()
        if parser is None:
            return None

        try:
            raw_patch = await parser.parse_patch(
                message=message,
                existing_criteria=existing_criteria,
            )
            patch = IntentCriteriaPatch.model_validate(raw_patch)
        except Exception:
            return None

        if not patch.has_values():
            return None
        return patch

    def _resolve_llm_parser(self) -> LLMIntentParserProtocol | None:
        if self._llm_parser_resolved:
            return self._llm_parser

        self._llm_parser_resolved = True
        try:
            self._llm_parser = self._llm_parser_factory()
        except Exception:
            self._llm_parser = None
        return self._llm_parser

    def _build_search_criteria(
        self,
        *,
        user_id: int,
        patch: IntentCriteriaPatch,
        locations: ResolvedLocations,
    ) -> SearchCriteria:
        deal_type = patch.deal_type or self._default_deal_type
        # A mentioned rent term («посуточно») implies rent even without the word.
        if patch.rent_period is not None and patch.deal_type is None:
            deal_type = "rent"
        return SearchCriteria(
            user_id=user_id,
            city=locations.city,
            deal_type=deal_type,
            rent_period=patch.rent_period if deal_type == "rent" else None,
            property_type="apartment",
            min_price_kzt=patch.min_price_kzt,
            max_price_kzt=patch.max_price_kzt,
            rooms=patch.rooms,
            districts=list(locations.districts) if locations.districts else None,
            min_area_m2=patch.min_area_m2,
            max_area_m2=patch.max_area_m2,
            owner_only=bool(patch.owner_only),
            page_limit=patch.page_limit or self._default_page_limit,
        )

    def _build_refined_criteria(
        self,
        *,
        criteria: SearchCriteria,
        patch: IntentCriteriaPatch,
        locations: ResolvedLocations,
    ) -> SearchCriteria:
        deal_type = patch.deal_type or criteria.deal_type
        # A mentioned rent term («посуточно») implies rent even without the word.
        if patch.rent_period is not None and patch.deal_type is None:
            deal_type = "rent"
        rent_period = patch.rent_period
        if rent_period is None and deal_type == "rent":
            rent_period = criteria.rent_period
        if deal_type != "rent":
            rent_period = None
        # Switching sale<->rent — or the rent term (300K/мес vs 15K/сутки) —
        # invalidates the old budget; unless the same message names a new one,
        # drop it so the user sets a budget that matches the new terms.
        deal_changed = deal_type != criteria.deal_type
        period_changed = deal_type == "rent" and rent_period != criteria.rent_period
        budget_stale = deal_changed or period_changed
        inherited_min = None if budget_stale else criteria.min_price_kzt
        inherited_max = None if budget_stale else criteria.max_price_kzt
        return SearchCriteria(
            user_id=criteria.user_id,
            city=locations.city,
            deal_type=deal_type,
            rent_period=rent_period,
            property_type=criteria.property_type,
            min_price_kzt=(
                inherited_min if patch.min_price_kzt is None else patch.min_price_kzt
            ),
            max_price_kzt=(
                inherited_max if patch.max_price_kzt is None else patch.max_price_kzt
            ),
            rooms=criteria.rooms if patch.rooms is None else patch.rooms,
            districts=list(locations.districts) if locations.districts else None,
            min_area_m2=criteria.min_area_m2 if patch.min_area_m2 is None else patch.min_area_m2,
            max_area_m2=criteria.max_area_m2 if patch.max_area_m2 is None else patch.max_area_m2,
            owner_only=criteria.owner_only if patch.owner_only is None else patch.owner_only,
            page_limit=criteria.page_limit if patch.page_limit is None else patch.page_limit,
        )
