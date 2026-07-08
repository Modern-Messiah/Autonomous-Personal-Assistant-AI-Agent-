"""Pure HTML parsing for Krisha.kz listing and detail pages (no network/browser I/O)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from agent.models.apartment import Apartment

BASE_URL = "https://krisha.kz"
CAPTCHA_MARKERS = ("captcha", "verify you are human", "too many requests", "access denied")
EXTERNAL_ID_PATTERN = re.compile(r"/a/show/(\d+)")
PRICE_PATTERN = re.compile(r"(\d[\d\s]{2,}\d)")
# Require a square-meter unit (м²/м2/m²/m2/кв.м) so values like the ceiling
# height "потолки 2.7м" are not mistaken for the apartment area.
AREA_PATTERN = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:кв\.?\s*м|м\s*²|м\s*2|m\s*²|m\s*2)",
    re.IGNORECASE,
)
FLOOR_PATTERN = re.compile(r"(\d+\s*/\s*\d+)")
# Tallest residential building in KZ is ~50 floors; anything above is a building
# number or parse noise, not a floor.
MAX_PLAUSIBLE_FLOOR = 60
ROOMS_PATTERN = re.compile(r"(\d+)\s*[- ]?ком")
ROOMS_WORD_PATTERN = re.compile(
    r"\u043a\u043e\u043c\u043d\u0430\u0442\w*\s*[:\-]?\s*(\d+)",
    re.IGNORECASE,
)
PUBLISHED_PATTERN = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
# krisha embeds the advert dates in JSON: `createdAt` is the ORIGINAL publish
# date — the honest "days on market" anchor — while `addedAt` resets to today on
# every seller re-bump, so it always looks fresh. Prefer createdAt.
CREATED_AT_PATTERN = re.compile(r'"createdAt"\s*:\s*"(\d{4}-\d{2}-\d{2})')
ADDED_AT_PATTERN = re.compile(r'"addedAt"\s*:\s*"(\d{4}-\d{2}-\d{2})')
# The page <title> ("... №<id>: <address> — за <price> — Крыша") is server-side
# rendered and always present, so it is the reliable source for the address that
# 2GIS geocodes (the in-page address node is JS-hydrated and often missing).
TITLE_ADDRESS_PATTERN = re.compile(
    r"№\d+:\s*(?P<addr>.+?)\s*[\u2014\u2013-]\s*за\s",
    re.IGNORECASE,
)

# Who posted the listing now lives in an embedded advert JSON object rather than
# the old `data-testid="advert-author"` markup (krisha dropped it, ~2026-07). The
# `"owner":{...}` object carries boolean flags (isOwner / isBuilder / isComplex)
# and a label; the agency name for a realtor sits in a sibling `"agency":{...}`.
# krisha's price-vs-similar verdict, worded like "<...> 9.2% cheaper than others".
MARKET_DIFF_PATTERN = re.compile(
    r"На\s+([\d]+(?:[.,]\d+)?)\s*%\s*(дешевле|дороже)",  # noqa: RUF001
    re.IGNORECASE,
)
CEILING_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)")
# Longest real descriptions are ~2k chars; cap guards against spam blobs.
DESCRIPTION_MAX_CHARS = 2000

# Real listing photos live on krisha's CDN under /webp/<hash>/<n>-<size>.jpg in
# many sizes; marketing banners sit under /content/ and must be skipped. krisha
# serves them from several kcdn hosts (krisha-photos.kcdn.online,
# alaps-photos-kr.kcdn.kz, ...), so match any of them. We read straight off the
# raw HTML (script/srcset/data-*), because the JS-hydrated <img src> attributes
# are not reliably populated when the page is read.
PHOTO_URL_PATTERN = re.compile(
    r"https://[\w.-]*kcdn\.[a-z]+/webp/[^\s\"'<>\\]+?-(?:\d+x\d+|full)\.(?:jpg|jpeg|png)",
    re.IGNORECASE,
)
PHOTO_SIZE_PATTERN = re.compile(
    r"^(?P<base>.+)-(?:\d+x\d+|full)\.(?:jpg|jpeg|png)$",
    re.IGNORECASE,
)
# krisha CDN serves every size for a photo by suffix; normalize to one good size.
PHOTO_DISPLAY_SIZE = "750x470"


class AntiBotBlockedError(RuntimeError):
    """Raised when target page appears blocked by anti-bot checks."""


@dataclass(slots=True, frozen=True)
class ListingPreview:
    """Preview listing extracted from search page."""

    external_id: str
    url: str
    title: str
    price_kzt: int | None
    rooms: int | None
    area_m2: float | None
    floor: str | None
    district: str | None
    address: str | None = None


@dataclass(slots=True)
class ParserHealthReport:
    """Outcome of a parser canary run against a reference search page."""

    ok: bool
    listing_count: int
    previews_with_price: int
    previews_with_specs: int
    detail_checked: bool
    failures: list[str]


class KrishaHtmlParser:
    """Stateless parser turning Krisha listing/detail HTML into domain objects."""

    def parse_listing_page(self, html: str) -> list[ListingPreview]:
        """Parse listing page HTML into preview objects."""
        if self._is_blocked_page(html):
            raise AntiBotBlockedError("Captcha or anti-bot marker detected on listing page.")

        soup = BeautifulSoup(html, "html.parser")
        previews: list[ListingPreview] = []
        seen_ids: set[str] = set()

        for link in soup.select('a[href*="/a/show/"]'):
            href_value = link.get("href")
            if not isinstance(href_value, str):
                continue
            external_id = self._extract_external_id(href_value)
            if external_id is None or external_id in seen_ids:
                continue

            card = self._resolve_card_container(link)
            title = self._first_non_empty(
                [
                    self._get_selector_text(card, ".a-card__title"),
                    self._get_selector_text(card, "h2"),
                    self._get_selector_text(card, "h3"),
                    link.get_text(" ", strip=True),
                ]
            )
            if title is None:
                title = f"Apartment {external_id}"

            price_text = self._first_non_empty(
                [
                    self._get_selector_text(card, ".a-card__price"),
                    self._get_selector_text(card, ".offer__price"),
                    self._get_selector_text(card, '[data-test="price"]'),
                ]
            )
            subtitle_text = self._get_selector_text(card, ".a-card__subtitle")
            details_text = self._first_non_empty(
                [
                    self._get_selector_text(card, ".a-card__text-preview"),
                    self._get_selector_text(card, ".offer__parameters"),
                ]
            )
            params_text = self._first_non_empty(
                [
                    " ".join(chunk for chunk in [subtitle_text, details_text] if chunk),
                    subtitle_text,
                    details_text,
                ]
            )
            # Krisha card titles carry clean structured specs, e.g.
            # "2-комнатная квартира · 58.5 м² · 6/9 этаж". Prefer the title for
            # rooms/area/floor so noisy details (ceiling height, building number
            # in the address) are not mistaken for them.
            spec_text = " ".join(chunk for chunk in [title, params_text] if chunk)

            preview = ListingPreview(
                external_id=external_id,
                url=self._normalize_url(href_value),
                title=title,
                price_kzt=self._extract_price_kzt(price_text),
                rooms=self._extract_rooms(spec_text),
                area_m2=self._extract_area(spec_text),
                floor=self._extract_floor(spec_text),
                district=(
                    self._extract_district(subtitle_text) or self._extract_district(params_text)
                ),
                address=subtitle_text,
            )
            previews.append(preview)
            seen_ids.add(external_id)

        return previews

    def parse_detail_page(self, html: str, *, preview: ListingPreview, city: str) -> Apartment:
        """Parse one listing detail page into Apartment."""
        if self._is_blocked_page(html):
            raise AntiBotBlockedError(
                f"Captcha or anti-bot marker detected for listing {preview.external_id}."
            )

        soup = BeautifulSoup(html, "html.parser")

        title = self._first_non_empty(
            [
                self._get_selector_text(soup, "h1.offer__title"),
                self._get_selector_text(soup, "h1"),
                preview.title,
            ]
        )
        if title is None:
            msg = f"Cannot parse title for listing {preview.external_id}"
            raise ValueError(msg)

        price_kzt = self._extract_price_kzt(
            self._first_non_empty(
                [
                    self._get_selector_text(soup, ".offer__price"),
                    self._get_selector_text(soup, '[data-test="offer-price"]'),
                ]
            )
        )
        if price_kzt is None:
            price_kzt = preview.price_kzt
        if price_kzt is None:
            msg = f"Cannot parse price for listing {preview.external_id}"
            raise ValueError(msg)

        address = self._first_non_empty(
            [
                self._get_selector_text(soup, ".offer__address"),
                self._get_selector_text(soup, '[data-test="address"]'),
                # krisha dropped the dedicated address node and the listing-card
                # subtitle is JS-hydrated (flaky), so prefer the SSR page <title>,
                # then fall back to the subtitle carried from the preview.
                self._extract_address_from_title(soup),
                preview.address,
            ]
        )
        detail_text = self._first_non_empty(
            [
                self._get_selector_text(soup, ".offer__parameters"),
                self._get_selector_text(soup, ".offer__info"),
                soup.get_text(" ", strip=True),
            ]
        )

        rooms = self._extract_rooms(detail_text) if preview.rooms is None else preview.rooms
        area_m2 = self._extract_area(detail_text) if preview.area_m2 is None else preview.area_m2
        floor = self._extract_floor(detail_text) if preview.floor is None else preview.floor

        photo_urls = self._extract_photo_urls(html)
        published_at = self._extract_published_at(soup, html)
        posted_by, agency_name = self._extract_author(html)
        params = self._extract_params(soup)

        return Apartment(
            external_id=preview.external_id,
            source="krisha",
            url=preview.url,
            title=title,
            price_kzt=price_kzt,
            city=city.strip(),
            district=self._extract_district(address) or preview.district,
            address=address,
            area_m2=area_m2,
            floor=floor,
            rooms=rooms,
            posted_by=posted_by,
            agency_name=agency_name,
            description=self._extract_description(soup),
            market_diff_percent=self._extract_market_diff(html),
            build_year=self._param_int(params, "год постройки"),
            building_type=self._param_value(params, "тип дома"),
            ceiling_height_m=self._param_float(params, "высота потолков"),
            furnished=self._param_value(params, "меблирова"),
            condition=self._param_value(params, "состояние"),
            photos=photo_urls,
            published_at=published_at,
        )

    @staticmethod
    def _extract_description(soup: BeautifulSoup) -> str | None:
        """The free-text description body (not the about-flat params block)."""
        node = soup.select_one(".js-description") or soup.select_one(".offer__description .text")
        if node is None:
            return None
        text = node.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            return None
        return text[:DESCRIPTION_MAX_CHARS]

    @staticmethod
    def _extract_market_diff(html: str) -> float | None:
        """krisha's price-vs-city verdict as a signed percent (cheaper = negative)."""
        match = MARKET_DIFF_PATTERN.search(html)
        if match is None:
            return None
        value = float(match.group(1).replace(",", "."))
        return -value if match.group(2).lower() == "дешевле" else value

    @staticmethod
    def _extract_params(soup: BeautifulSoup) -> dict[str, str]:
        """Flatten both about-flat param blocks into a {lower label: value} map.

        Desktop krisha has TWO param areas: a <dl> (balcony/door/furnished/ceiling)
        and info-item rows (city/building type/build year/condition), where the
        value is an `.offer__advert-short-info` node in the same row as the title.
        """
        params: dict[str, str] = {}
        for dl in soup.find_all("dl"):
            terms = dl.find_all("dt")
            values = dl.find_all("dd")
            for term, value in zip(terms, values, strict=False):
                label = term.get_text(" ", strip=True).lower()
                text = value.get_text(" ", strip=True)
                if label and text:
                    params.setdefault(label, text)
        for title in soup.select(".offer__info-title"):
            row = title.parent
            value_node = row.select_one(".offer__advert-short-info") if row else None
            if value_node is None:
                continue
            label = title.get_text(" ", strip=True).lower()
            text = value_node.get_text(" ", strip=True)
            if label and text:
                params.setdefault(label, text)
        return params

    @staticmethod
    def _param_value(params: dict[str, str], needle: str) -> str | None:
        for label, value in params.items():
            if needle in label:
                return value
        return None

    @classmethod
    def _param_int(cls, params: dict[str, str], needle: str) -> int | None:
        value = cls._param_value(params, needle)
        if value is None:
            return None
        match = re.search(r"\d{4}", value)
        return int(match.group()) if match else None

    @classmethod
    def _param_float(cls, params: dict[str, str], needle: str) -> float | None:
        value = cls._param_value(params, needle)
        if value is None:
            return None
        match = CEILING_PATTERN.search(value)
        return float(match.group(1).replace(",", ".")) if match else None

    @classmethod
    def _extract_author(
        cls,
        html: str,
    ) -> tuple[Literal["owner", "agent", "developer"] | None, str | None]:
        """Return (posted_by, agency_name) from the embedded advert JSON.

        The poster is the `"owner":{...}` object: `isOwner` → owner (Хозяин),
        `isBuilder`/`isComplex` → developer (застройщик/новостройка), otherwise a
        realtor/specialist → agent, whose name comes from the sibling
        `"agency":{"name": ...}` object.
        """
        owner = cls._find_json_object(html, "owner", required_key="isOwner")
        if owner is None:
            return None, None
        if owner.get("isOwner"):
            return "owner", None
        if owner.get("isBuilder") or owner.get("isComplex"):
            return "developer", None
        agency = cls._find_json_object(html, "agency", required_key="name")
        name = agency.get("name") if agency is not None else None
        if not isinstance(name, str):
            return "agent", None
        return "agent", name.strip() or None

    @staticmethod
    def _find_json_object(html: str, key: str, *, required_key: str) -> dict[str, Any] | None:
        """First embedded ``"key": {...}`` JSON object that carries ``required_key``.

        Brace-matches the object so a nested child (e.g. the label) doesn't
        truncate it; skips objects that don't parse or lack the marker key.
        """
        for match in re.finditer(rf'"{re.escape(key)}"\s*:\s*{{', html):
            start = html.index("{", match.start())
            depth = 0
            for index in range(start, min(len(html), start + 8000)):
                char = html[index]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(html[start : index + 1])
                        except json.JSONDecodeError:
                            break
                        if isinstance(parsed, dict) and required_key in parsed:
                            return parsed
                        break
        return None

    def _deduplicate_previews(self, previews: list[ListingPreview]) -> list[ListingPreview]:
        """Drop exact id repeats AND collapse the same flat re-posted by different
        agents (same building+rooms+area+floor under different ids), keeping the
        cheapest listing so clones don't pad the shortlist or skew the average."""
        result: list[ListingPreview] = []
        seen_ids: set[str] = set()
        fingerprint_index: dict[tuple[int, float, str, str], int] = {}
        for preview in previews:
            if preview.external_id in seen_ids:
                continue
            seen_ids.add(preview.external_id)
            fingerprint = self._listing_fingerprint(preview)
            if fingerprint is not None and fingerprint in fingerprint_index:
                position = fingerprint_index[fingerprint]
                if self._price_sort_key(preview) < self._price_sort_key(result[position]):
                    result[position] = preview
                continue
            if fingerprint is not None:
                fingerprint_index[fingerprint] = len(result)
            result.append(preview)
        return result

    @staticmethod
    def _listing_fingerprint(
        preview: ListingPreview,
    ) -> tuple[int, float, str, str] | None:
        """Identity of the flat itself, independent of who posted it. None when a
        component is missing OR the address has no house number — a street-only
        address ("Абая, Алматы") could match two different buildings, so we would
        rather keep both than risk hiding a distinct flat."""
        if (
            preview.rooms is None
            or preview.area_m2 is None
            or not preview.floor
            or not preview.address
            or not any(char.isdigit() for char in preview.address)
        ):
            return None
        address = " ".join(preview.address.lower().split())
        return (preview.rooms, round(preview.area_m2, 1), preview.floor.replace(" ", ""), address)

    @staticmethod
    def _price_sort_key(preview: ListingPreview) -> tuple[int, int]:
        """Cheapest first; a known price beats an unknown one."""
        if preview.price_kzt is None:
            return (1, 0)
        return (0, preview.price_kzt)

    def _resolve_card_container(self, link: object) -> object:
        node = link
        fallback = link
        for _ in range(6):
            parent = getattr(node, "parent", None)
            if parent is None:
                break
            class_values = parent.get("class", [])
            if isinstance(class_values, list):
                classes = " ".join(str(value) for value in class_values).lower()
                if "a-card" in classes or "offer" in classes:
                    # The single card root holds exactly one subtitle; inner
                    # wrappers (entered via the image anchor) hold none and the
                    # results list holds many. Prefer the one-subtitle root so
                    # subtitle/district/address resolve regardless of which nested
                    # anchor we matched first.
                    select = getattr(parent, "select", None)
                    if callable(select) and len(select(".a-card__subtitle")) == 1:
                        return parent
                    fallback = parent
            node = parent
        return fallback

    @staticmethod
    def _normalize_url(href: str) -> str:
        # Drop tracking query/fragment (e.g. ?srchid=...&srchtype=hot_block_filter
        # &srchpos=2&source=search_advert). On promoted "hot block" adverts those
        # params send the detail page into a redirect loop (ERR_TOO_MANY_REDIRECTS);
        # the bare /a/show/<id> path is the canonical, fetchable URL.
        absolute = urljoin(BASE_URL, href)
        split = urlsplit(absolute)
        return urlunsplit((split.scheme, split.netloc, split.path, "", ""))

    @staticmethod
    def _extract_external_id(href: str) -> str | None:
        match = EXTERNAL_ID_PATTERN.search(href)
        if match is None:
            return None
        return match.group(1)

    @staticmethod
    def _is_blocked_page(html: str) -> bool:
        lowered = html.lower()
        if not any(marker in lowered for marker in CAPTCHA_MARKERS):
            return False
        # krisha appends a reCAPTCHA legal footer ("защищён
        # сервисом reCAPTCHA") to every normal page, so a
        # bare marker match would flag valid result pages as blocked. A page that still renders
        # real listing/offer content is therefore never a genuine anti-bot interstitial.
        has_listings = "/a/show/" in lowered or "a-card" in lowered
        has_offer = "offer__price" in lowered or "offer__title" in lowered
        return not (has_listings or has_offer)

    @staticmethod
    def _get_selector_text(container: object, selector: str) -> str | None:
        node = container.select_one(selector)  # type: ignore[attr-defined]
        if node is None:
            return None
        text = node.get_text(" ", strip=True)
        return text or None

    @staticmethod
    def _first_non_empty(values: list[str | None]) -> str | None:
        for value in values:
            if value:
                return value
        return None

    @staticmethod
    def _extract_int(text: str | None, pattern: re.Pattern[str]) -> int | None:
        if text is None:
            return None
        match = pattern.search(text.replace("\xa0", " "))
        if match is None:
            return None
        digits = re.sub(r"\D", "", match.group(1))
        if not digits:
            return None
        return int(digits)

    def _extract_price_kzt(self, text: str | None) -> int | None:
        return self._extract_int(text, PRICE_PATTERN)

    def _extract_rooms(self, text: str | None) -> int | None:
        rooms = self._extract_int(text, ROOMS_PATTERN)
        if rooms is not None:
            return rooms
        return self._extract_int(text, ROOMS_WORD_PATTERN)

    @staticmethod
    def _extract_area(text: str | None) -> float | None:
        if text is None:
            return None
        match = AREA_PATTERN.search(text.replace("\xa0", " "))
        if match is None:
            return None
        return float(match.group(1).replace(",", "."))

    @staticmethod
    def _extract_floor(text: str | None) -> str | None:
        """Return "floor/total", ignoring implausible «N/M» (e.g. a building number
        like «535/2» leaking from the address — floor 535 of 2 is impossible)."""
        if text is None:
            return None
        for match in FLOOR_PATTERN.finditer(text):
            current_str, _, total_str = match.group(1).replace(" ", "").partition("/")
            try:
                current, total = int(current_str), int(total_str)
            except ValueError:
                continue
            if 1 <= current <= total <= MAX_PLAUSIBLE_FLOOR:
                return f"{current}/{total}"
        return None

    @staticmethod
    def _extract_district(text: str | None) -> str | None:
        if text is None:
            return None
        normalized = text.replace("\xa0", " ").strip()
        parts = [part.strip() for part in re.split(r"[,\|]", normalized) if part.strip()]
        for part in parts:
            lowered = part.lower()
            if "\u0440-\u043d" in lowered or "\u0440\u0430\u0439\u043e\u043d" in lowered:
                return part
        return None

    @staticmethod
    def _extract_address_from_title(soup: BeautifulSoup) -> str | None:
        node = soup.find("title")
        if node is None:
            return None
        match = TITLE_ADDRESS_PATTERN.search(node.get_text(" ", strip=True))
        if match is None:
            return None
        address = match.group("addr").strip()
        return address or None

    @staticmethod
    def _extract_photo_urls(html: str) -> list[str]:
        """Return real listing photos (one best-size URL per distinct photo).

        Matches CDN photo URLs straight from the raw HTML so it does not depend on
        JS-hydrated ``<img src>`` (which is often empty when the page is read).
        Skips marketing banners (under /content/) and collapses the many size
        variants krisha emits down to one normalized display size per photo.
        """
        bases: list[str] = []
        seen: set[str] = set()
        for url in PHOTO_URL_PATTERN.findall(html):
            match = PHOTO_SIZE_PATTERN.match(url)
            if match is None:
                continue
            base = match.group("base")
            if base not in seen:
                seen.add(base)
                bases.append(base)

        return [f"{base}-{PHOTO_DISPLAY_SIZE}.jpg" for base in bases]

    @classmethod
    def _extract_published_at(cls, soup: BeautifulSoup, html: str) -> datetime | None:
        """First-published date, preferring the advert JSON `createdAt`.

        krisha dropped the `time[datetime]` markup and lets sellers re-bump
        (`addedAt` → today), so `createdAt` is the honest post date. Fall back to
        `addedAt`, then to the legacy markup for older/cached pages.
        """
        for pattern in (CREATED_AT_PATTERN, ADDED_AT_PATTERN):
            match = pattern.search(html)
            if match is not None:
                try:
                    return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=UTC)
                except ValueError:
                    continue

        for selector in ("time[datetime]", "[data-test='published-at'][datetime]"):
            node = soup.select_one(selector)
            if node is None:
                continue
            datetime_attr = node.get("datetime")
            if not isinstance(datetime_attr, str):
                continue
            try:
                dt = datetime.fromisoformat(datetime_attr)
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=UTC)
                return dt
            except ValueError:
                continue

        text = soup.get_text(" ", strip=True)
        match = PUBLISHED_PATTERN.search(text)
        if match is None:
            return None
        try:
            parsed = datetime.strptime(match.group(1), "%d.%m.%Y")
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC)
