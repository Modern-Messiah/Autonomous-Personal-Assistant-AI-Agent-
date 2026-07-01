"""Pure HTML parsing for Krisha.kz listing and detail pages (no network/browser I/O)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin

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
ROOMS_PATTERN = re.compile(r"(\d+)\s*[- ]?ком")
ROOMS_WORD_PATTERN = re.compile(
    r"\u043a\u043e\u043c\u043d\u0430\u0442\w*\s*[:\-]?\s*(\d+)",
    re.IGNORECASE,
)
PUBLISHED_PATTERN = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
# The page <title> ("... №<id>: <address> — за <price> — Крыша") is server-side
# rendered and always present, so it is the reliable source for the address that
# 2GIS geocodes (the in-page address node is JS-hydrated and often missing).
TITLE_ADDRESS_PATTERN = re.compile(
    r"№\d+:\s*(?P<addr>.+?)\s*[\u2014\u2013-]\s*за\s",
    re.IGNORECASE,
)

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
        published_at = self._extract_published_at(soup)

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
            photos=photo_urls,
            published_at=published_at,
        )

    def _deduplicate_previews(self, previews: list[ListingPreview]) -> list[ListingPreview]:
        unique: list[ListingPreview] = []
        seen_ids: set[str] = set()
        for preview in previews:
            if preview.external_id in seen_ids:
                continue
            unique.append(preview)
            seen_ids.add(preview.external_id)
        return unique

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
        return urljoin(BASE_URL, href)

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
        if text is None:
            return None
        match = FLOOR_PATTERN.search(text)
        if match is None:
            return None
        return match.group(1).replace(" ", "")

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

    @staticmethod
    def _extract_published_at(soup: BeautifulSoup) -> datetime | None:
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
