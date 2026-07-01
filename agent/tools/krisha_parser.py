"""Krisha.kz parser: Playwright fetch + anti-bot + Redis dedup, delegating HTML parsing.

Pure HTML parsing lives in :mod:`agent.tools.krisha_html`; this module owns the
browser I/O, deduplication, criteria filtering, and orchestration.
"""

from __future__ import annotations

import asyncio
import random
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import quote, urlencode

from fake_useragent import UserAgent
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.tools.districts import canonical_district
from agent.tools.krisha_html import (
    BASE_URL,
    AntiBotBlockedError,
    KrishaHtmlParser,
    ListingPreview,
    ParserHealthReport,
)

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext

# Re-export the parsing types that used to live here so existing imports of
# `agent.tools.krisha_parser` keep working after the HTML parser was split out.
__all__ = [
    "BASE_URL",
    "AntiBotBlockedError",
    "BrowserContextProtocol",
    "KrishaParser",
    "ListingPreview",
    "PageProtocol",
    "ParserHealthReport",
    "RedisSetProtocol",
    "ResponseProtocol",
    "UserAgentProvider",
    "build_redis_client",
]


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
        dedup_namespace: str = "search",
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
        self._dedup_namespace = dedup_namespace
        self._html = KrishaHtmlParser()

    async def create_browser_context(self, browser: Browser) -> BrowserContext:
        """Create Playwright context with randomized user-agent."""
        user_agent = self._user_agent_provider.get()
        return await browser.new_context(user_agent=user_agent, locale="ru-RU")

    def parse_listing_page(self, html: str) -> list[ListingPreview]:
        """Delegate to the pure HTML parser (kept for the public/test surface)."""
        return self._html.parse_listing_page(html)

    def parse_detail_page(self, html: str, *, preview: ListingPreview, city: str) -> Apartment:
        """Delegate to the pure HTML parser (kept for the public/test surface)."""
        return self._html.parse_detail_page(html, preview=preview, city=city)

    def _deduplicate_previews(self, previews: list[ListingPreview]) -> list[ListingPreview]:
        return self._html._deduplicate_previews(previews)

    def _is_blocked_page(self, html: str) -> bool:
        return self._html._is_blocked_page(html)

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
            preview for preview in deduped_previews if self._matches_criteria(preview, criteria)
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

    async def check_health(
        self,
        context: BrowserContextProtocol,
        *,
        criteria: SearchCriteria,
    ) -> ParserHealthReport:
        """Parse a reference search page and verify key fields still extract.

        This is the canary: it exercises the same parsing code the search uses
        (listing previews + one detail page) without claiming dedup keys, so it
        catches a krisha markup change or a block before users hit empty results.
        Raises AntiBotBlockedError when krisha serves a captcha/anti-bot page, so
        the caller can report a block distinctly from a markup regression.
        """
        listing_url = self._build_listing_urls(criteria)[0]
        page = await context.new_page()
        try:
            html = await self._fetch_page_html(page, listing_url)
        finally:
            await page.close()

        previews = self.parse_listing_page(html)
        failures: list[str] = []
        if not previews:
            failures.append("no previews parsed from the listing page (selectors changed?)")

        with_price = sum(1 for preview in previews if preview.price_kzt is not None)
        with_specs = sum(
            1 for preview in previews if preview.rooms is not None or preview.area_m2 is not None
        )
        if previews and with_price == 0:
            failures.append("no preview carried a price (price parsing broke)")
        if previews and with_specs == 0:
            failures.append("no preview carried rooms or area (spec parsing broke)")

        detail_checked = False
        if previews:
            first = previews[0]
            page = await context.new_page()
            apartment: Apartment | None = None
            try:
                detail_html = await self._fetch_page_html(page, first.url)
                apartment = self.parse_detail_page(detail_html, preview=first, city=criteria.city)
            except ValueError as exc:
                failures.append(f"detail page failed to parse ({exc})")
            finally:
                await page.close()
            detail_checked = True
            if apartment is not None:
                if not apartment.photos:
                    failures.append("detail page yielded no photos (photo extraction broke)")
                if apartment.address is None:
                    failures.append("detail page yielded no address (address parsing broke)")

        return ParserHealthReport(
            ok=not failures,
            listing_count=len(previews),
            previews_with_price=with_price,
            previews_with_specs=with_specs,
            detail_checked=detail_checked,
            failures=failures,
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

    @staticmethod
    def _matches_criteria(preview: ListingPreview, criteria: SearchCriteria) -> bool:
        """Check a listing-card preview against search criteria.

        Unknown preview fields (``None``) are treated as a match so listings with
        a sparse card are not dropped before the detail page is fetched. Districts
        are resolved to a city-scoped canonical name on both sides (requested vs the
        card's Russian label); a listing is dropped only when its district is known
        and not among the requested ones. If the requested districts can't be
        resolved for the city (unmapped city/district), district filtering is
        skipped, so "city without a district" returns the whole city.
        """
        rooms = preview.rooms
        if criteria.rooms and rooms is not None and rooms not in criteria.rooms:
            return False

        if criteria.districts:
            wanted = {canonical_district(name, criteria.city) for name in criteria.districts}
            wanted.discard(None)
            if wanted:
                found = canonical_district(preview.district, criteria.city) or canonical_district(
                    preview.address, criteria.city
                )
                if found is not None and found not in wanted:
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

    def _dedup_key(self, *, user_id: int, external_id: str) -> str:
        if self._dedup_namespace == "search":
            return f"krisha:seen:{user_id}:{external_id}"
        return f"krisha:seen:{self._dedup_namespace}:{user_id}:{external_id}"


def build_redis_client(redis_url: str) -> RedisSetProtocol:
    """Build redis client lazily to avoid import cost in modules that do not need it."""
    redis_module = import_module("redis.asyncio")
    client = redis_module.from_url(redis_url, decode_responses=True)
    return cast(RedisSetProtocol, client)
