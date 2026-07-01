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
from config.settings import get_settings

PRICE_UNIT_MILLION = r"(?:млн|миллион\w*|m)"
PRICE_UNIT_THOUSAND = r"(?:тыс\w*|k)"
PRICE_UNIT_TENGE = r"(?:тг|тенге|₸)"
PRICE_VALUE_PATTERN = re.compile(
    rf"(\d+(?:[.,]\d+)?)\s*({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})",
    re.IGNORECASE,
)
PRICE_RANGE_PATTERN = re.compile(
    rf"(?:от\s+)?(\d+(?:[.,]\d+)?)\s*"
    rf"({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})\s*"
    rf"(?:-|\u2013|до|to)\s*"
    rf"(\d+(?:[.,]\d+)?)\s*({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})",
    re.IGNORECASE,
)
PRICE_MIN_PATTERN = re.compile(
    rf"(?:от|min|from)\s+(\d+(?:[.,]\d+)?)\s*"
    rf"({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})",
    re.IGNORECASE,
)
PRICE_MAX_PATTERN = re.compile(
    rf"(?:до|max|не\s+дороже)\s+(\d+(?:[.,]\d+)?)\s*"
    rf"({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})",
    re.IGNORECASE,
)
AREA_RANGE_PATTERN = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:-|\u2013|до|to)\s*(\d+(?:[.,]\d+)?)\s*(?:м2|м²|m2)",
    re.IGNORECASE,
)
AREA_MIN_PATTERN = re.compile(
    r"(?:от|min|from)\s+(\d+(?:[.,]\d+)?)\s*(?:м2|м²|m2)",
    re.IGNORECASE,
)
AREA_MAX_PATTERN = re.compile(
    r"(?:до|max)\s+(\d+(?:[.,]\d+)?)\s*(?:м2|м²|m2)",
    re.IGNORECASE,
)
ROOMS_RANGE_PATTERN = re.compile(
    r"(\d+)\s*(?:-|\u2013)\s*(\d+)\s*(?:ком\w*|room\w*)",
    re.IGNORECASE,
)
ROOMS_SINGLE_PATTERN = re.compile(
    r"(\d+)[-\u2013\s]*(?:ком\w*|room\w*)",
    re.IGNORECASE,
)
ROOMS_OR_PATTERN = re.compile(r"(\d+)\s*(?:или|or)\s*(\d+)", re.IGNORECASE)
# Word-number room counts so the spelled-out forms parse the same as "2-комнатная".
# Both spellings (with and without the "yo" letter) are keyed so lookup is a plain
# lowercased match with no character folding.
_WORD_ROOMS: dict[str, int] = {
    "одно": 1,
    "двух": 2,
    "трех": 3,
    "трёх": 3,
    "четырех": 4,
    "четырёх": 4,
    "пяти": 5,
    "шести": 6,
}
ROOMS_WORD_PATTERN = re.compile(
    r"(одно|двух|трёх|трех|четырёх|четырех|пяти|шести)[-\s]*ком\w*",
    re.IGNORECASE,
)
# Colloquial single words (odnushka / dvushka / tryoshka / chetyryoshka).
_SLANG_ROOMS: dict[str, int] = {
    "однушк": 1,
    "двушк": 2,
    "трешк": 3,
    "трёшк": 3,
    "четырешк": 4,
    "четырёшк": 4,
}
ROOMS_SLANG_PATTERN = re.compile(
    r"(однушк|двушк|трёшк|трешк|четырёшк|четырешк)\w*",
    re.IGNORECASE,
)
PAGE_LIMIT_PATTERN = re.compile(
    r"(?:pages?|page_limit|страниц\w*)\s*(\d+)",
    re.IGNORECASE,
)

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
    min_price_kzt: int | None = Field(default=None, ge=0)
    max_price_kzt: int | None = Field(default=None, ge=0)
    rooms: list[int] | None = None
    districts: list[str] | None = None
    min_area_m2: float | None = Field(default=None, ge=0)
    max_area_m2: float | None = Field(default=None, ge=0)
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
                "min_price_kzt",
                "max_price_kzt",
                "rooms",
                "districts",
                "min_area_m2",
                "max_area_m2",
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


def create_default_llm_intent_parser() -> LLMIntentParserProtocol | None:
    """Create the production LLM parser from settings, or disable it safely."""
    try:
        settings = get_settings()
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
            criteria=self._parse_with_regex(
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
        return self._refine_with_regex(
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
        return SearchCriteria(
            user_id=user_id,
            city=locations.city,
            deal_type=patch.deal_type or self._default_deal_type,
            property_type="apartment",
            min_price_kzt=patch.min_price_kzt,
            max_price_kzt=patch.max_price_kzt,
            rooms=patch.rooms,
            districts=list(locations.districts) if locations.districts else None,
            min_area_m2=patch.min_area_m2,
            max_area_m2=patch.max_area_m2,
            page_limit=patch.page_limit or self._default_page_limit,
        )

    def _build_refined_criteria(
        self,
        *,
        criteria: SearchCriteria,
        patch: IntentCriteriaPatch,
        locations: ResolvedLocations,
    ) -> SearchCriteria:
        return SearchCriteria(
            user_id=criteria.user_id,
            city=locations.city,
            deal_type=patch.deal_type or criteria.deal_type,
            property_type=criteria.property_type,
            min_price_kzt=(
                criteria.min_price_kzt
                if patch.min_price_kzt is None
                else patch.min_price_kzt
            ),
            max_price_kzt=(
                criteria.max_price_kzt
                if patch.max_price_kzt is None
                else patch.max_price_kzt
            ),
            rooms=criteria.rooms if patch.rooms is None else patch.rooms,
            districts=list(locations.districts) if locations.districts else None,
            min_area_m2=criteria.min_area_m2 if patch.min_area_m2 is None else patch.min_area_m2,
            max_area_m2=criteria.max_area_m2 if patch.max_area_m2 is None else patch.max_area_m2,
            page_limit=criteria.page_limit if patch.page_limit is None else patch.page_limit,
        )

    def _parse_with_regex(
        self,
        *,
        user_id: int,
        message: str,
        locations: ResolvedLocations,
    ) -> SearchCriteria:
        normalized = message.strip().lower()
        deal_type = self._parse_deal_type(normalized)
        min_price, max_price = self._parse_price_bounds(normalized)
        min_area, max_area = self._parse_area_bounds(normalized)
        rooms = self._parse_rooms(normalized)
        page_limit = self._parse_page_limit(normalized)

        return SearchCriteria(
            user_id=user_id,
            city=locations.city,
            deal_type=deal_type,
            property_type="apartment",
            min_price_kzt=min_price,
            max_price_kzt=max_price,
            rooms=rooms,
            districts=list(locations.districts) if locations.districts else None,
            min_area_m2=min_area,
            max_area_m2=max_area,
            page_limit=page_limit,
        )

    def _refine_with_regex(
        self,
        *,
        criteria: SearchCriteria,
        message: str,
        locations: ResolvedLocations,
    ) -> SearchCriteria:
        normalized = message.strip().lower()
        deal_type = self._find_deal_type(normalized)
        min_price, max_price = self._parse_price_bounds(normalized)
        min_area, max_area = self._parse_area_bounds(normalized)
        rooms = self._parse_rooms(normalized)
        page_limit = self._find_page_limit(normalized)

        return SearchCriteria(
            user_id=criteria.user_id,
            city=locations.city,
            deal_type=deal_type or criteria.deal_type,
            property_type=criteria.property_type,
            min_price_kzt=criteria.min_price_kzt if min_price is None else min_price,
            max_price_kzt=criteria.max_price_kzt if max_price is None else max_price,
            rooms=criteria.rooms if rooms is None else rooms,
            districts=list(locations.districts) if locations.districts else None,
            min_area_m2=criteria.min_area_m2 if min_area is None else min_area,
            max_area_m2=criteria.max_area_m2 if max_area is None else max_area,
            page_limit=criteria.page_limit if page_limit is None else page_limit,
        )

    def _parse_deal_type(self, text: str) -> Literal["sale", "rent"]:
        deal_type = self._find_deal_type(text)
        if deal_type is not None:
            return deal_type
        return self._default_deal_type

    def _find_deal_type(self, text: str) -> Literal["sale", "rent"] | None:
        rent_markers = ("аренд", "снят", "rent")
        if any(marker in text for marker in rent_markers):
            return "rent"
        sale_markers = ("куп", "покуп", "sale", "buy")
        if any(marker in text for marker in sale_markers):
            return "sale"
        return None

    def _parse_price_bounds(self, text: str) -> tuple[int | None, int | None]:
        range_match = PRICE_RANGE_PATTERN.search(text)
        if range_match is not None:
            range_min = self._to_kzt(range_match.group(1), range_match.group(2))
            range_max = self._to_kzt(range_match.group(3), range_match.group(4))
            if range_min is not None and range_max is not None and range_min <= range_max:
                return range_min, range_max

        min_price: int | None = None
        max_price: int | None = None

        min_match = PRICE_MIN_PATTERN.search(text)
        if min_match is not None:
            min_price = self._to_kzt(min_match.group(1), min_match.group(2))

        max_match = PRICE_MAX_PATTERN.search(text)
        if max_match is not None:
            max_price = self._to_kzt(max_match.group(1), max_match.group(2))

        if min_price is None and max_price is None:
            price_values = [
                self._to_kzt(amount, unit)
                for amount, unit in PRICE_VALUE_PATTERN.findall(text)
            ]
            concrete = [value for value in price_values if value is not None]
            if len(concrete) == 1:
                max_price = concrete[0]

        if min_price is not None and max_price is not None and min_price > max_price:
            return max_price, min_price
        return min_price, max_price

    def _parse_area_bounds(self, text: str) -> tuple[float | None, float | None]:
        range_match = AREA_RANGE_PATTERN.search(text)
        if range_match is not None:
            range_min = self._to_float(range_match.group(1))
            range_max = self._to_float(range_match.group(2))
            if range_min is not None and range_max is not None and range_min <= range_max:
                return range_min, range_max

        min_area: float | None = None
        max_area: float | None = None

        min_match = AREA_MIN_PATTERN.search(text)
        if min_match is not None:
            min_area = self._to_float(min_match.group(1))

        max_match = AREA_MAX_PATTERN.search(text)
        if max_match is not None:
            max_area = self._to_float(max_match.group(1))

        if min_area is not None and max_area is not None and min_area > max_area:
            return max_area, min_area
        return min_area, max_area

    def _parse_rooms(self, text: str) -> list[int] | None:
        rooms: set[int] = set()

        range_match = ROOMS_RANGE_PATTERN.search(text)
        if range_match is not None:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start <= end:
                rooms.update(range(start, end + 1))

        for left, right in ROOMS_OR_PATTERN.findall(text):
            rooms.add(int(left))
            rooms.add(int(right))

        for room_str in ROOMS_SINGLE_PATTERN.findall(text):
            rooms.add(int(room_str))

        for word in ROOMS_WORD_PATTERN.findall(text):
            count = _WORD_ROOMS.get(word.lower())
            if count is not None:
                rooms.add(count)

        for word in ROOMS_SLANG_PATTERN.findall(text):
            count = _SLANG_ROOMS.get(word.lower())
            if count is not None:
                rooms.add(count)

        cleaned = sorted(room for room in rooms if room > 0)
        return cleaned or None

    def _parse_page_limit(self, text: str) -> int:
        parsed = self._find_page_limit(text)
        if parsed is not None:
            return parsed
        return self._default_page_limit

    def _find_page_limit(self, text: str) -> int | None:
        match = PAGE_LIMIT_PATTERN.search(text)
        if match is None:
            return None
        parsed = int(match.group(1))
        return min(max(parsed, 1), 20)

    @staticmethod
    def _to_float(value: str) -> float | None:
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None

    @staticmethod
    def _to_kzt(number_text: str, unit_text: str) -> int | None:
        try:
            value = float(number_text.replace(",", "."))
        except ValueError:
            return None

        lowered_unit = unit_text.lower()
        if "млн" in lowered_unit or "миллион" in lowered_unit or lowered_unit == "m":
            return int(value * 1_000_000)
        if "тыс" in lowered_unit or lowered_unit == "k":
            return int(value * 1_000)
        return int(value)
