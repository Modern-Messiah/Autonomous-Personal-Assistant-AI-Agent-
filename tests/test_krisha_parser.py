"""Tests for Krisha parser tool."""

from pathlib import Path

import pytest

from agent.models.criteria import SearchCriteria
from agent.tools.krisha_parser import KrishaParser, ResponseProtocol


def load_fixture(name: str) -> str:
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "krisha" / name
    return fixture_path.read_text(encoding="utf-8")


class FakeResponse(ResponseProtocol):
    """Minimal response stub."""

    def __init__(self, status: int) -> None:
        self.status = status


class FakePage:
    """In-memory page object that returns predefined HTML by URL."""

    def __init__(self, page_map: dict[str, tuple[int, str]]) -> None:
        self._page_map = page_map
        self._current_url: str | None = None

    async def goto(
        self,
        url: str,
        *,
        wait_until: str,
        **kwargs: object,
    ) -> FakeResponse | None:
        del wait_until, kwargs
        if url not in self._page_map:
            msg = f"Unexpected url requested in test: {url}"
            raise AssertionError(msg)
        self._current_url = url
        status, _ = self._page_map[url]
        return FakeResponse(status=status)

    async def content(self) -> str:
        if self._current_url is None:
            msg = "content() called before goto()"
            raise AssertionError(msg)
        _, html = self._page_map[self._current_url]
        return html

    async def close(self) -> None:
        return None


class FakeBrowserContext:
    """Context that creates fake pages."""

    def __init__(self, page_map: dict[str, tuple[int, str]]) -> None:
        self._page_map = page_map

    async def new_page(self) -> FakePage:
        return FakePage(self._page_map)


class FakeRedis:
    """Set-if-not-exists behavior for dedup tests."""

    def __init__(self, seen_keys: set[str] | None = None) -> None:
        self._seen_keys = seen_keys or set()

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> bool | None:
        del value, ex
        if nx and name in self._seen_keys:
            return False
        self._seen_keys.add(name)
        return True


def build_criteria(page_limit: int = 1) -> SearchCriteria:
    return SearchCriteria(
        user_id=1,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        page_limit=page_limit,
    )


@pytest.mark.asyncio
async def test_search_parses_listing_and_detail_pages() -> None:
    listing_html = load_fixture("listing_page.html")
    detail_html = load_fixture("detail_123456789.html")

    redis = FakeRedis(seen_keys={"krisha:seen:987654321"})
    parser = KrishaParser(
        redis_client=redis,
        min_delay_seconds=0,
        max_delay_seconds=0,
        timeout_ms=10_000,
    )
    criteria = build_criteria(page_limit=1)
    listing_url = parser._build_listing_urls(criteria)[0]

    page_map = {
        listing_url: (200, listing_html),
        "https://krisha.kz/a/show/123456789": (200, detail_html),
    }
    context = FakeBrowserContext(page_map)

    apartments = await parser.search(context, criteria)

    assert len(apartments) == 1
    apartment = apartments[0]
    assert apartment.external_id == "123456789"
    assert apartment.price_kzt == 35_000_000
    assert apartment.rooms == 2
    assert apartment.area_m2 == 58.5
    assert apartment.floor == "6/9"
    assert apartment.district is not None
    assert apartment.district.endswith("\u0440-\u043d")
    assert len(apartment.photos) == 2


@pytest.mark.asyncio
async def test_search_returns_empty_on_captcha_page() -> None:
    captcha_html = load_fixture("captcha_page.html")

    redis = FakeRedis()
    parser = KrishaParser(
        redis_client=redis,
        min_delay_seconds=0,
        max_delay_seconds=0,
    )
    criteria = build_criteria(page_limit=1)
    listing_url = parser._build_listing_urls(criteria)[0]
    context = FakeBrowserContext({listing_url: (200, captcha_html)})

    apartments = await parser.search(context, criteria)

    assert apartments == []


def test_parse_listing_page_extracts_previews() -> None:
    listing_html = load_fixture("listing_page.html")
    parser = KrishaParser(redis_client=FakeRedis(), min_delay_seconds=0, max_delay_seconds=0)

    previews = parser.parse_listing_page(listing_html)

    assert len(previews) == 2
    assert previews[0].external_id == "123456789"
    assert previews[0].price_kzt == 35_000_000
    assert previews[1].external_id == "987654321"
    assert previews[1].rooms == 1
