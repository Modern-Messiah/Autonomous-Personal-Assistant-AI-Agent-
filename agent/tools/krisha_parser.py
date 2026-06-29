"""Playwright-first parser for Krisha.kz listings."""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import quote, urlencode, urljoin

from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext

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

# Real listing photos live on the krisha CDN under /webp/<hash>/<n>-<size>.jpg in
# many sizes; marketing banners sit under /content/ and must be skipped.
LISTING_PHOTO_HOST = "krisha-photos.kcdn.online"
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


class ResponseProtocol(Protocol):
    """Minimal response protocol compatible with Playwright response."""

    status: int


class PageProtocol(Protocol):
    """Minimal async page protocol for testability."""

    async def goto(
        self,
        url: str,
        *,
        wait_until: str,
        **kwargs: object,
    ) -> ResponseProtocol | None: ...

    async def content(self) -> str: ...
    async def close(self) -> None: ...


class BrowserContextProtocol(Protocol):
    """Minimal browser context protocol for creating pages."""

    async def new_page(self) -> PageProtocol: ...


class RedisSetProtocol(Protocol):
    """Redis subset used for deduplication."""

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> bool | None: ...

    async def delete(self, *names: str) -> int: ...


class UserAgentProvider:
    """Returns randomized user-agent values."""

    def __init__(self, fallback_pool: tuple[str, ...] | None = None) -> None:
        self._fallback_pool = fallback_pool or (
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
            ),
            (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        try:
            self._generator: UserAgent | None = UserAgent()
        except Exception:
            self._generator = None

    def get(self) -> str:
        """Return one randomized UA string."""
        if self._generator is not None:
            try:
                return str(self._generator.random)
            except Exception:
                pass
        return random.choice(self._fallback_pool)


class KrishaParser:
    """Krisha.kz parser with anti-ban controls and Redis deduplication."""

    def __init__(
        self,
        *,
        redis_client: RedisSetProtocol,
        user_agent_provider: UserAgentProvider | None = None,
        min_delay_seconds: float = 1.0,
        max_delay_seconds: float = 3.0,
        timeout_ms: int = 30_000,
        dedup_ttl_seconds: int = 86_400,
        max_results: int = 12,
    ) -> None:
        if min_delay_seconds > max_delay_seconds:
            msg = "min_delay_seconds cannot be greater than max_delay_seconds"
            raise ValueError(msg)
        if max_results < 1:
            msg = "max_results must be at least 1"
            raise ValueError(msg)
        self._redis = redis_client
        self._user_agent_provider = user_agent_provider or UserAgentProvider()
        self._min_delay_seconds = min_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._timeout_ms = timeout_ms
        self._dedup_ttl_seconds = dedup_ttl_seconds
        self._max_results = max_results

    async def create_browser_context(self, browser: Browser) -> BrowserContext:
        """Create Playwright context with randomized user-agent."""
        user_agent = self._user_agent_provider.get()
        return await browser.new_context(user_agent=user_agent, locale="ru-RU")

    async def search(
        self,
        context: BrowserContextProtocol,
        criteria: SearchCriteria,
    ) -> list[Apartment]:
        """Search Krisha and return structured apartments."""
        listing_urls = self._build_listing_urls(criteria)
        previews: list[ListingPreview] = []
        last_listing_timeout: PlaywrightTimeoutError | None = None

        for listing_url in listing_urls:
            page = await context.new_page()
            try:
                html = await self._fetch_page_html(page, listing_url)
                previews.extend(self.parse_listing_page(html))
            except AntiBotBlockedError:
                # Propagate so callers can tell "blocked by anti-bot" apart from
                # "genuinely nothing found" and message the user accordingly
                # (page is closed by the finally below).
                raise
            except PlaywrightTimeoutError as exc:
                last_listing_timeout = exc
            finally:
                await page.close()
            await self._sleep_between_requests()

        if not previews and last_listing_timeout is not None:
            raise last_listing_timeout

        deduped_previews = self._deduplicate_previews(previews)
        # krisha ignores our listing-page filter params, so apply the criteria
        # client-side on the parsed previews. This also bounds work: only the
        # first `max_results` matching listings are fetched in detail, instead of
        # crawling every listing on every page just to show a short shortlist.
        matching_previews = [
            preview
            for preview in deduped_previews
            if self._matches_criteria(preview, criteria)
        ]

        apartments: list[Apartment] = []
        for preview in matching_previews:
            if len(apartments) >= self._max_results:
                break
            claimed = await self._claim_preview(
                user_id=criteria.user_id,
                external_id=preview.external_id,
            )
            if not claimed:
                continue
            page = await context.new_page()
            try:
                html = await self._fetch_page_html(page, preview.url)
                apartment = self.parse_detail_page(html, preview=preview, city=criteria.city)
            except AntiBotBlockedError:
                await self._release_preview(
                    user_id=criteria.user_id,
                    external_id=preview.external_id,
                )
            except PlaywrightTimeoutError:
                await self._release_preview(
                    user_id=criteria.user_id,
                    external_id=preview.external_id,
                )
            except Exception:
                await self._release_preview(
                    user_id=criteria.user_id,
                    external_id=preview.external_id,
                )
                raise
            else:
                apartments.append(apartment)
            finally:
                await page.close()
            await self._sleep_between_requests()

        return apartments

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

        photo_urls = self._extract_photo_urls(soup)
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

    def _build_listing_urls(self, criteria: SearchCriteria) -> list[str]:
        city_slug = quote(criteria.city.strip().lower().replace(" ", "-"))
        segment = "prodazha" if criteria.deal_type == "sale" else "arenda"
        base_url = f"{BASE_URL}/{segment}/kvartiry/{city_slug}/"

        urls: list[str] = []
        for page in range(1, criteria.page_limit + 1):
            params: dict[str, str] = {"page": str(page)}
            if criteria.min_price_kzt is not None:
                params["price_from"] = str(criteria.min_price_kzt)
            if criteria.max_price_kzt is not None:
                params["price_to"] = str(criteria.max_price_kzt)
            if criteria.rooms:
                params["rooms"] = ",".join(str(room) for room in criteria.rooms)
            if criteria.min_area_m2 is not None:
                params["area_from"] = str(criteria.min_area_m2)
            if criteria.max_area_m2 is not None:
                params["area_to"] = str(criteria.max_area_m2)
            if criteria.districts:
                params["districts"] = ",".join(criteria.districts)
            urls.append(f"{base_url}?{urlencode(params)}")
        return urls

    async def _fetch_page_html(self, page: PageProtocol, url: str) -> str:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        if response is not None and response.status == 429:
            raise AntiBotBlockedError(f"Received HTTP 429 for {url}")
        html = await page.content()
        if self._is_blocked_page(html):
            raise AntiBotBlockedError(f"Anti-bot marker detected for {url}")
        return html

    async def _sleep_between_requests(self) -> None:
        delay = random.uniform(self._min_delay_seconds, self._max_delay_seconds)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _claim_preview(self, *, user_id: int, external_id: str) -> bool:
        dedup_key = self._dedup_key(user_id=user_id, external_id=external_id)
        is_new = await self._redis.set(dedup_key, "1", ex=self._dedup_ttl_seconds, nx=True)
        return bool(is_new)

    async def _release_preview(self, *, user_id: int, external_id: str) -> None:
        dedup_key = self._dedup_key(user_id=user_id, external_id=external_id)
        await self._redis.delete(dedup_key)

    def _deduplicate_previews(self, previews: list[ListingPreview]) -> list[ListingPreview]:
        unique: list[ListingPreview] = []
        seen_ids: set[str] = set()
        for preview in previews:
            if preview.external_id in seen_ids:
                continue
            unique.append(preview)
            seen_ids.add(preview.external_id)
        return unique

    @staticmethod
    def _matches_criteria(preview: ListingPreview, criteria: SearchCriteria) -> bool:
        """Check a listing-card preview against search criteria.

        Unknown preview fields (``None``) are treated as a match so listings with
        a sparse card are not dropped before the detail page is fetched. Districts
        are not filtered here: criteria use canonical English names while previews
        carry Russian district labels, so matching is unreliable.
        """
        rooms = preview.rooms
        if criteria.rooms and rooms is not None and rooms not in criteria.rooms:
            return False

        price = preview.price_kzt
        if price is not None:
            if criteria.min_price_kzt is not None and price < criteria.min_price_kzt:
                return False
            if criteria.max_price_kzt is not None and price > criteria.max_price_kzt:
                return False

        area = preview.area_m2
        if area is not None:
            if criteria.min_area_m2 is not None and area < criteria.min_area_m2:
                return False
            if criteria.max_area_m2 is not None and area > criteria.max_area_m2:
                return False

        return True

    def _resolve_card_container(self, link: object) -> object:
        node = link
        for _ in range(5):
            parent = getattr(node, "parent", None)
            if parent is None:
                break
            class_values = parent.get("class", [])
            if isinstance(class_values, list):
                classes = " ".join(str(value) for value in class_values).lower()
                if "a-card" in classes or "offer" in classes:
                    return parent
            node = parent
        return link

    @staticmethod
    def _normalize_url(href: str) -> str:
        return urljoin(BASE_URL, href)

    @staticmethod
    def _dedup_key(*, user_id: int, external_id: str) -> str:
        return f"krisha:seen:{user_id}:{external_id}"

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
    def _extract_photo_urls(soup: BeautifulSoup) -> list[str]:
        """Return real listing photos (one best-size URL per distinct photo).

        Skips marketing banners (/content/) and the dozens of duplicate size
        variants krisha emits, so the bot does not show logos/thumbnails.
        """
        bases: list[str] = []
        seen: set[str] = set()
        for img in soup.select("img"):
            candidate = img.get("src") or img.get("data-src")
            if not isinstance(candidate, str):
                continue
            url = urljoin(BASE_URL, candidate)
            if LISTING_PHOTO_HOST not in url or "/content/" in url:
                continue
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


def build_redis_client(redis_url: str) -> RedisSetProtocol:
    """Build redis client lazily to avoid import cost in modules that do not need it."""
    redis_module = import_module("redis.asyncio")
    client = redis_module.from_url(redis_url, decode_responses=True)
    return cast(RedisSetProtocol, client)
