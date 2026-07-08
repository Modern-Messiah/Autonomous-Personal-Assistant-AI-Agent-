"""Deterministic regex parser for search criteria — the LLM-free fallback.

Extracted from ``IntentNode`` so the node stays an orchestrator (LLM path +
fallback strategy) while the ~450 lines of pattern machinery live and evolve
here. ``find_rent_period``/``find_owner_only`` are also used as backstops on
top of LLM output — «от хозяина» / «посуточно» must work even if the LLM
missed them.
"""

from __future__ import annotations

import re
from typing import Literal

from agent.locations import ResolvedLocations
from agent.models.criteria import SearchCriteria

PRICE_UNIT_MILLION = r"(?:млн|миллион\w*|m)"
PRICE_UNIT_THOUSAND = r"(?:тыс\w*|k)"
PRICE_UNIT_TENGE = r"(?:тг|тенге|₸)"
PRICE_VALUE_PATTERN = re.compile(
    rf"(\d+(?:[.,]\d+)?)\s*({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})",
    re.IGNORECASE,
)
PRICE_RANGE_PATTERN = re.compile(
    rf"(?:от\s+)?(\d+(?:[.,]\d+)?)\s*"
    # The first bound's unit may be omitted («от 30 до 50 млн») — it then
    # inherits the second bound's unit in _parse_price_bounds.
    rf"({PRICE_UNIT_MILLION}|{PRICE_UNIT_THOUSAND}|{PRICE_UNIT_TENGE})?\s*"
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


class RegexIntentParser:
    """Turns free-form Russian/English apartment queries into SearchCriteria."""

    def __init__(
        self,
        *,
        default_deal_type: Literal["sale", "rent"] = "sale",
        default_page_limit: int = 3,
    ) -> None:
        self._default_deal_type = default_deal_type
        self._default_page_limit = default_page_limit

    def parse(
        self,
        *,
        user_id: int,
        message: str,
        locations: ResolvedLocations,
    ) -> SearchCriteria:
        normalized = message.strip().lower()
        deal_type = self._parse_deal_type(normalized)
        rent_period = self.find_rent_period(normalized)
        if rent_period is not None:
            deal_type = "rent"
        min_price, max_price = self._parse_price_bounds(normalized)
        min_area, max_area = self._parse_area_bounds(normalized)
        rooms = self._parse_rooms(normalized)
        page_limit = self._parse_page_limit(normalized)

        return SearchCriteria(
            user_id=user_id,
            city=locations.city,
            deal_type=deal_type,
            rent_period=rent_period,
            property_type="apartment",
            min_price_kzt=min_price,
            max_price_kzt=max_price,
            rooms=rooms,
            districts=list(locations.districts) if locations.districts else None,
            min_area_m2=min_area,
            max_area_m2=max_area,
            owner_only=bool(self.find_owner_only(normalized)),
            page_limit=page_limit,
        )

    def refine(
        self,
        *,
        criteria: SearchCriteria,
        message: str,
        locations: ResolvedLocations,
    ) -> SearchCriteria:
        normalized = message.strip().lower()
        deal_type = self.find_deal_type(normalized)
        found_period = self.find_rent_period(normalized)
        if found_period is not None and deal_type is None:
            deal_type = "rent"
        min_price, max_price = self._parse_price_bounds(normalized)
        min_area, max_area = self._parse_area_bounds(normalized)
        rooms = self._parse_rooms(normalized)
        page_limit = self._find_page_limit(normalized)
        owner_only = self.find_owner_only(normalized)

        new_deal_type = deal_type or criteria.deal_type
        rent_period = found_period
        if rent_period is None and new_deal_type == "rent":
            rent_period = criteria.rent_period
        if new_deal_type != "rent":
            rent_period = None
        # Same rule as the LLM path: switching sale<->rent — or the rent term
        # (месяц vs сутки vs час) — drops the old budget unless this message
        # names a new one.
        deal_changed = new_deal_type != criteria.deal_type
        period_changed = new_deal_type == "rent" and rent_period != criteria.rent_period
        budget_stale = deal_changed or period_changed
        inherited_min = None if budget_stale else criteria.min_price_kzt
        inherited_max = None if budget_stale else criteria.max_price_kzt
        return SearchCriteria(
            user_id=criteria.user_id,
            city=locations.city,
            deal_type=new_deal_type,
            rent_period=rent_period,
            property_type=criteria.property_type,
            min_price_kzt=inherited_min if min_price is None else min_price,
            max_price_kzt=inherited_max if max_price is None else max_price,
            rooms=criteria.rooms if rooms is None else rooms,
            districts=list(locations.districts) if locations.districts else None,
            min_area_m2=criteria.min_area_m2 if min_area is None else min_area,
            max_area_m2=criteria.max_area_m2 if max_area is None else max_area,
            owner_only=criteria.owner_only if owner_only is None else owner_only,
            page_limit=criteria.page_limit if page_limit is None else page_limit,
        )

    def _parse_deal_type(self, text: str) -> Literal["sale", "rent"]:
        deal_type = self.find_deal_type(text)
        if deal_type is not None:
            return deal_type
        return self._default_deal_type

    @staticmethod
    def find_deal_type(text: str) -> Literal["sale", "rent"] | None:
        # "снят"/"сним" cover снять/снял/сниму/снимем/снимать declensions.
        rent_markers = ("аренд", "снят", "сним", "rent")
        if any(marker in text for marker in rent_markers):
            return "rent"
        sale_markers = ("куп", "покуп", "sale", "buy")
        if any(marker in text for marker in sale_markers):
            return "sale"
        return None

    @staticmethod
    def find_rent_period(text: str) -> Literal["monthly", "daily", "hourly"] | None:
        """Detect the rent term; a mentioned term also implies deal_type=rent."""
        lowered = text.lower()
        if "посуточ" in lowered or "на сутки" in lowered:
            return "daily"
        if "по часам" in lowered or "почасов" in lowered:
            return "hourly"
        if "помесяч" in lowered or "на месяц" in lowered or "долгосрочн" in lowered:
            return "monthly"
        return None

    @staticmethod
    def find_owner_only(text: str) -> bool | None:
        """Detect the owner-only request; None when the message doesn't mention it."""
        owner_markers = (
            "от хозяина",
            "от хозяев",
            "от собственника",
            "от собственников",
            "без риелтор",
            "без риэлтор",
            "без посредник",
            "без агент",
        )
        lowered = text.lower()
        if any(marker in lowered for marker in owner_markers):
            return True
        return None

    def _parse_price_bounds(self, text: str) -> tuple[int | None, int | None]:
        range_match = PRICE_RANGE_PATTERN.search(text)
        if range_match is not None:
            # «от 30 до 50 млн»: the first bound has no unit — use the second's.
            min_unit = range_match.group(2) or range_match.group(4)
            range_min = self._to_kzt(range_match.group(1), min_unit)
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
