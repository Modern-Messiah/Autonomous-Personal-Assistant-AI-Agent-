"""Intent node that converts user text into SearchCriteria."""

from __future__ import annotations

import re
from typing import Literal, TypedDict

from agent.models.criteria import SearchCriteria

PRICE_UNIT_MILLION = (
    r"(?:\u043c\u043b\u043d|\u043c\u0438\u043b\u043b\u0438\u043e\u043d\w*|m)"
)
PRICE_UNIT_THOUSAND = r"(?:\u0442\u044b\u0441\w*|k)"
PRICE_UNIT_TENGE = r"(?:\u0442\u0433|\u0442\u0435\u043d\u0433\u0435|\u20b8)"
PRICE_VALUE_PATTERN = re.compile(
    rf"(\d+(?:[.,]\d+)?)\s*({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})",
    re.IGNORECASE,
)
PRICE_RANGE_PATTERN = re.compile(
    rf"(?:\u043e\u0442\s+)?(\d+(?:[.,]\d+)?)\s*"
    rf"({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})\s*"
    rf"(?:-|\u2013|\u0434\u043e|to)\s*"
    rf"(\d+(?:[.,]\d+)?)\s*({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})",
    re.IGNORECASE,
)
PRICE_MIN_PATTERN = re.compile(
    rf"(?:\u043e\u0442|min|from)\s+(\d+(?:[.,]\d+)?)\s*"
    rf"({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})",
    re.IGNORECASE,
)
PRICE_MAX_PATTERN = re.compile(
    rf"(?:\u0434\u043e|max|\u043d\u0435\s+\u0434\u043e\u0440\u043e\u0436\u0435)\s+"
    rf"(\d+(?:[.,]\d+)?)\s*({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})",
    re.IGNORECASE,
)
AREA_RANGE_PATTERN = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:-|\u2013|\u0434\u043e|to)\s*(\d+(?:[.,]\d+)?)\s*(?:\u043c2|\u043c\u00b2|m2)",
    re.IGNORECASE,
)
AREA_MIN_PATTERN = re.compile(
    r"(?:\u043e\u0442|min|from)\s+(\d+(?:[.,]\d+)?)\s*(?:\u043c2|\u043c\u00b2|m2)",
    re.IGNORECASE,
)
AREA_MAX_PATTERN = re.compile(
    r"(?:\u0434\u043e|max)\s+(\d+(?:[.,]\d+)?)\s*(?:\u043c2|\u043c\u00b2|m2)",
    re.IGNORECASE,
)
ROOMS_RANGE_PATTERN = re.compile(
    r"(\d+)\s*(?:-|\u2013)\s*(\d+)\s*(?:\u043a\u043e\u043c\w*|room\w*)",
    re.IGNORECASE,
)
ROOMS_SINGLE_PATTERN = re.compile(r"(\d+)\s*(?:\u043a\u043e\u043c\w*|room\w*)", re.IGNORECASE)
ROOMS_OR_PATTERN = re.compile(r"(\d+)\s*(?:\u0438\u043b\u0438|or)\s*(\d+)", re.IGNORECASE)
PAGE_LIMIT_PATTERN = re.compile(
    r"(?:pages?|page_limit|\u0441\u0442\u0440\u0430\u043d\u0438\u0446\w*)\s*(\d+)",
    re.IGNORECASE,
)

CITY_ALIASES = {
    "almaty": "Almaty",
    "\u0430\u043b\u043c\u0430\u0442": "Almaty",
    "astana": "Astana",
    "\u0430\u0441\u0442\u0430\u043d": "Astana",
    "\u043d\u0443\u0440-\u0441\u0443\u043b\u0442\u0430\u043d": "Astana",
    "shymkent": "Shymkent",
    "\u0448\u044b\u043c\u043a\u0435\u043d\u0442": "Shymkent",
}
DISTRICT_ALIASES = {
    "bostandyk": "Bostandyk",
    "\u0431\u043e\u0441\u0442\u0430\u043d\u0434\u044b\u043a": "Bostandyk",
    "medeu": "Medeu",
    "\u043c\u0435\u0434\u0435\u0443": "Medeu",
    "auezov": "Auezov",
    "\u0430\u0443\u044d\u0437\u043e\u0432": "Auezov",
    "almaly": "Almaly",
    "\u0430\u043b\u043c\u0430\u043b": "Almaly",
    "nauryzbay": "Nauryzbay",
    "\u043d\u0430\u0443\u0440\u044b\u0437\u0431\u0430\u0439": "Nauryzbay",
    "turksib": "Turksib",
    "\u0442\u0443\u0440\u043a\u0441\u0438\u0431": "Turksib",
    "zhetysu": "Zhetysu",
    "\u0436\u0435\u0442\u044b\u0441\u0443": "Zhetysu",
}


class IntentState(TypedDict, total=False):
    """State used by intent node."""

    user_id: int
    message: str
    criteria: SearchCriteria


class IntentNode:
    """Rule-based text parser for search criteria."""

    def __init__(
        self,
        *,
        default_city: str = "Almaty",
        default_deal_type: Literal["sale", "rent"] = "sale",
        default_page_limit: int = 3,
    ) -> None:
        self._default_city = default_city
        self._default_deal_type = default_deal_type
        self._default_page_limit = default_page_limit

    async def __call__(self, state: IntentState) -> IntentState:
        criteria = self.parse(user_id=state["user_id"], message=state["message"])
        return {
            "user_id": state["user_id"],
            "message": state["message"],
            "criteria": criteria,
        }

    def parse(self, *, user_id: int, message: str) -> SearchCriteria:
        """Parse free-form message into SearchCriteria."""
        normalized = message.strip().lower()
        deal_type = self._parse_deal_type(normalized)
        city = self._parse_city(normalized)
        min_price, max_price = self._parse_price_bounds(normalized)
        min_area, max_area = self._parse_area_bounds(normalized)
        rooms = self._parse_rooms(normalized)
        districts = self._parse_districts(normalized)
        page_limit = self._parse_page_limit(normalized)

        return SearchCriteria(
            user_id=user_id,
            city=city,
            deal_type=deal_type,
            property_type="apartment",
            min_price_kzt=min_price,
            max_price_kzt=max_price,
            rooms=rooms,
            districts=districts,
            min_area_m2=min_area,
            max_area_m2=max_area,
            page_limit=page_limit,
        )

    def refine(self, *, criteria: SearchCriteria, message: str) -> SearchCriteria:
        """Merge free-form refinement text into existing criteria."""
        normalized = message.strip().lower()
        deal_type = self._find_deal_type(normalized)
        city = self._find_city(normalized)
        min_price, max_price = self._parse_price_bounds(normalized)
        min_area, max_area = self._parse_area_bounds(normalized)
        rooms = self._parse_rooms(normalized)
        districts = self._parse_districts(normalized)
        page_limit = self._find_page_limit(normalized)

        return SearchCriteria(
            user_id=criteria.user_id,
            city=city or criteria.city,
            deal_type=deal_type or criteria.deal_type,
            property_type=criteria.property_type,
            min_price_kzt=criteria.min_price_kzt if min_price is None else min_price,
            max_price_kzt=criteria.max_price_kzt if max_price is None else max_price,
            rooms=criteria.rooms if rooms is None else rooms,
            districts=criteria.districts if districts is None else districts,
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
        rent_markers = (
            "\u0430\u0440\u0435\u043d\u0434",
            "\u0441\u043d\u044f\u0442",
            "rent",
        )
        if any(marker in text for marker in rent_markers):
            return "rent"
        sale_markers = (
            "\u043a\u0443\u043f",
            "\u043f\u043e\u043a\u0443\u043f",
            "sale",
            "buy",
        )
        if any(marker in text for marker in sale_markers):
            return "sale"
        return None

    def _parse_city(self, text: str) -> str:
        city = self._find_city(text)
        if city is not None:
            return city
        return self._default_city

    def _find_city(self, text: str) -> str | None:
        for alias, city in CITY_ALIASES.items():
            if alias in text:
                return city
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

        cleaned = sorted(room for room in rooms if room > 0)
        return cleaned or None

    def _parse_districts(self, text: str) -> list[str] | None:
        districts: list[str] = []
        seen: set[str] = set()
        for alias, canonical in DISTRICT_ALIASES.items():
            if alias in text and canonical not in seen:
                districts.append(canonical)
                seen.add(canonical)
        return districts or None

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
        if (
            "\u043c\u043b\u043d" in lowered_unit
            or "\u043c\u0438\u043b\u043b\u0438\u043e\u043d" in lowered_unit
            or lowered_unit == "m"
        ):
            return int(value * 1_000_000)
        if "\u0442\u044b\u0441" in lowered_unit or lowered_unit == "k":
            return int(value * 1_000)
        return int(value)
