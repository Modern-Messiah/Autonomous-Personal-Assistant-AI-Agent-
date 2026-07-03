"""Tests for Krisha parser tool."""

from collections.abc import Mapping
from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.tools.krisha_parser import (
    AntiBotBlockedError,
    KrishaParser,
    ListingPreview,
    ResponseProtocol,
)


def make_preview(
    *,
    external_id: str = "1",
    price_kzt: int | None = None,
    rooms: int | None = None,
    area_m2: float | None = None,
    district: str | None = None,
    address: str | None = None,
) -> ListingPreview:
    return ListingPreview(
        external_id=external_id,
        url=f"https://krisha.kz/a/show/{external_id}",
        title=f"Apartment {external_id}",
        price_kzt=price_kzt,
        rooms=rooms,
        area_m2=area_m2,
        floor=None,
        district=district,
        address=address,
    )


def load_fixture(name: str) -> str:
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "krisha" / name
    return fixture_path.read_text(encoding="utf-8")


class FakeResponse(ResponseProtocol):
    """Minimal response stub."""

    def __init__(self, status: int) -> None:
        self.status = status


class FakePage:
    """In-memory page object that returns predefined HTML by URL."""

    def __init__(self, page_map: Mapping[str, tuple[int, str] | Exception]) -> None:
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
        current = self._page_map[url]
        if isinstance(current, Exception):
            raise current
        self._current_url = url
        status, _ = current
        return FakeResponse(status=status)

    async def content(self) -> str:
        if self._current_url is None:
            msg = "content() called before goto()"
            raise AssertionError(msg)
        current = self._page_map[self._current_url]
        if isinstance(current, Exception):
            msg = "content() called for failed page load"
            raise AssertionError(msg)
        _, html = current
        return html

    async def close(self) -> None:
        return None


class FakeBrowserContext:
    """Context that creates fake pages."""

    def __init__(self, page_map: Mapping[str, tuple[int, str] | Exception]) -> None:
        self._page_map = page_map

    async def new_page(self) -> FakePage:
        return FakePage(self._page_map)


class FakeRedis:
    """Set-if-not-exists behavior for dedup tests."""

    def __init__(self, seen_keys: set[str] | None = None) -> None:
        self._seen_keys = seen_keys or set()
        self.deleted_keys: list[str] = []

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

    async def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            if name in self._seen_keys:
                self._seen_keys.remove(name)
                deleted += 1
            self.deleted_keys.append(name)
        return deleted


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

    redis = FakeRedis(seen_keys={"krisha:seen:1:987654321"})
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
    # Two distinct photos (size variants deduped, marketing banner skipped),
    # each resolved to the preferred 750x470 size.
    assert apartment.photos == [
        "https://krisha-photos.kcdn.online/webp/8d/8d9f-uuid/101-750x470.jpg",
        "https://krisha-photos.kcdn.online/webp/8d/8d9f-uuid/102-750x470.jpg",
    ]


def test_detail_page_extracts_author_kind() -> None:
    parser = KrishaParser(redis_client=FakeRedis(), min_delay_seconds=0, max_delay_seconds=0)
    base_html = load_fixture("detail_123456789.html")
    preview = make_preview(external_id="123456789", price_kzt=35_000_000)

    owner_block = (
        '<div data-testid="advert-author" class="a-page__block">'
        '<div class="owner" data-v-b16dc70c="">'
        '<h2 data-testid="advert-author-title">Хозяин недвижимости</h2></div></div>'
    )
    owner = parser.parse_detail_page(
        base_html.replace("</body>", owner_block + "</body>"),
        preview=preview, city="Almaty",
    )
    assert owner.posted_by == "owner"
    assert owner.agency_name is None

    company_block = (
        '<div data-testid="advert-author" class="a-page__block">'
        '<div class="company" data-v-7106868f="">'
        '<h2 data-testid="advert-author-title">Top City</h2></div></div>'
    )
    agent = parser.parse_detail_page(
        base_html.replace("</body>", company_block + "</body>"),
        preview=preview, city="Almaty",
    )
    assert agent.posted_by == "agent"
    assert agent.agency_name == "Top City"

    builder_block = (
        '<div data-testid="advert-author" class="a-page__block">'
        '<div class="builder" data-v-c2af3d64="">'
        '<div class="builder__header">ЖК Пример</div></div></div>'
    )
    developer = parser.parse_detail_page(
        base_html.replace("</body>", builder_block + "</body>"),
        preview=preview, city="Almaty",
    )
    assert developer.posted_by == "developer"
    assert developer.agency_name is None

    # no author block on the page -> both stay None
    plain = parser.parse_detail_page(base_html, preview=preview, city="Almaty")
    assert plain.posted_by is None
    assert plain.agency_name is None


def test_build_listing_urls_adds_rent_period() -> None:
    parser = KrishaParser(redis_client=FakeRedis(), min_delay_seconds=0, max_delay_seconds=0)
    rent = SearchCriteria(
        user_id=1, city="Almaty", deal_type="rent", property_type="apartment", page_limit=1
    )

    # monthly is krisha's default -> no param
    assert "rent.period" not in parser._build_listing_urls(rent)[0]
    monthly = rent.model_copy(update={"rent_period": "monthly"})
    assert "rent.period" not in parser._build_listing_urls(monthly)[0]

    daily = rent.model_copy(update={"rent_period": "daily"})
    assert "das%5Brent.period%5D=1" in parser._build_listing_urls(daily)[0]

    hourly = rent.model_copy(update={"rent_period": "hourly"})
    assert "das%5Brent.period%5D=4" in parser._build_listing_urls(hourly)[0]

    # a sale never carries the rent param even if the field is set
    sale = rent.model_copy(update={"deal_type": "sale", "rent_period": "daily"})
    assert "rent.period" not in parser._build_listing_urls(sale)[0]


def test_build_listing_urls_adds_owner_filter() -> None:
    parser = KrishaParser(redis_client=FakeRedis(), min_delay_seconds=0, max_delay_seconds=0)
    base = SearchCriteria(
        user_id=1, city="Almaty", deal_type="sale", property_type="apartment", page_limit=1
    )

    plain = parser._build_listing_urls(base)[0]
    assert "das%5Bwho%5D=1" not in plain

    owner = parser._build_listing_urls(base.model_copy(update={"owner_only": True}))[0]
    # krisha's "Кто разместил: от хозяев" server-side filter
    assert "das%5Bwho%5D=1" in owner


def test_listing_url_drops_tracking_query() -> None:
    # Promoted "hot block" cards carry tracking params that redirect-loop the
    # detail page; the parser must keep only the canonical /a/show/<id> path.
    parser = KrishaParser(redis_client=FakeRedis(), min_delay_seconds=0, max_delay_seconds=0)
    card = (
        '<div class="a-card">'
        '<a class="a-card__title" '
        'href="/a/show/123?srchid=abc&srchtype=hot_block_filter&srchpos=2&source=search_advert">'
        "2-комнатная квартира · 60 м² · 3/9 этаж</a>"
        '<div class="a-card__price">40 000 000 〒</div>'
        "</div>"
    )
    previews = parser.parse_listing_page(f"<html><body>{card}</body></html>")

    assert previews
    assert previews[0].url == "https://krisha.kz/a/show/123"
    assert previews[0].external_id == "123"


@pytest.mark.asyncio
async def test_search_skips_listing_when_detail_fetch_fails() -> None:
    # A single listing whose detail page fails to load (e.g. redirect loop) must
    # be skipped, not sink the whole search.
    cards = "".join(
        f'<div class="a-card">'
        f'<a class="a-card__title" href="/a/show/{external_id}">'
        f"2-комнатная квартира · 60 м² · 3/9 этаж</a>"
        f'<div class="a-card__price">40 000 000 〒</div>'
        f"</div>"
        for external_id in ("1", "2")
    )
    listing_html = f"<html><body>{cards}</body></html>"
    detail_html = load_fixture("detail_123456789.html")
    redis = FakeRedis()
    parser = KrishaParser(redis_client=redis, min_delay_seconds=0, max_delay_seconds=0)
    criteria = build_criteria(page_limit=1)
    listing_url = parser._build_listing_urls(criteria)[0]
    context = FakeBrowserContext(
        {
            listing_url: (200, listing_html),
            "https://krisha.kz/a/show/1": RuntimeError("net::ERR_TOO_MANY_REDIRECTS"),
            "https://krisha.kz/a/show/2": (200, detail_html),
        }
    )

    apartments = await parser.search(context, criteria)

    assert [apartment.external_id for apartment in apartments] == ["2"]
    assert "krisha:seen:1:1" in redis.deleted_keys  # skipped listing's claim released


@pytest.mark.asyncio
async def test_search_raises_on_captcha_page() -> None:
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

    # A blocked listing page must surface as an error, not an empty result, so
    # callers can distinguish it from "genuinely nothing found".
    with pytest.raises(AntiBotBlockedError):
        await parser.search(context, criteria)


def test_matches_criteria_filters_rooms_and_price() -> None:
    criteria = SearchCriteria(
        user_id=1, city="Almaty", deal_type="sale", property_type="apartment",
        rooms=[2], max_price_kzt=45_000_000,
    )
    # matches: 2-room, within budget
    assert KrishaParser._matches_criteria(
        make_preview(rooms=2, price_kzt=40_000_000), criteria
    )
    # wrong room count
    assert not KrishaParser._matches_criteria(
        make_preview(rooms=3, price_kzt=40_000_000), criteria
    )
    # over budget
    assert not KrishaParser._matches_criteria(
        make_preview(rooms=2, price_kzt=60_000_000), criteria
    )


def test_build_listing_urls_uses_city_slug() -> None:
    parser = KrishaParser(redis_client=FakeRedis(), min_delay_seconds=0, max_delay_seconds=0)
    for city, slug in [
        ("Almaty", "almaty"),
        ("Pavlodar", "pavlodar"),
        ("Ust-Kamenogorsk", "ust-kamenogorsk"),
        ("Semei", "semej"),
        ("Shchuchinsk", "shhuchinsk"),
        ("Tobyl", "zatobolsk"),
    ]:
        crit = SearchCriteria(
            user_id=1, city=city, deal_type="sale", property_type="apartment", page_limit=1
        )
        url = parser._build_listing_urls(crit)[0]
        assert f"/kvartiry/{slug}/" in url


def test_matches_criteria_filters_by_district() -> None:
    criteria = SearchCriteria(
        user_id=1, city="Almaty", deal_type="sale", property_type="apartment",
        districts=["Bostandyk"],
    )
    # the card's Russian label resolves to the requested district -> match
    assert KrishaParser._matches_criteria(make_preview(district="Бостандыкский район"), criteria)
    # a different district -> filtered out
    assert not KrishaParser._matches_criteria(make_preview(district="Медеуский район"), criteria)
    # district unknown on the card (and no address) -> kept, not dropped by mistake
    assert KrishaParser._matches_criteria(make_preview(district=None), criteria)


def test_matches_criteria_district_uses_address_fallback() -> None:
    criteria = SearchCriteria(
        user_id=1, city="Almaty", deal_type="sale", property_type="apartment",
        districts=["Medeu"],
    )
    assert KrishaParser._matches_criteria(
        make_preview(district=None, address="Алматы, Медеуский район, проспект Достык 1"),
        criteria,
    )
    assert not KrishaParser._matches_criteria(
        make_preview(district="Ауэзовский район"), criteria
    )


def test_matches_criteria_district_is_city_scoped() -> None:
    astana = SearchCriteria(
        user_id=1, city="Astana", deal_type="sale", property_type="apartment",
        districts=["Yesil"],
    )
    assert KrishaParser._matches_criteria(make_preview(district="Есильский район"), astana)
    assert not KrishaParser._matches_criteria(make_preview(district="Сарыаркинский район"), astana)


def test_matches_criteria_unresolved_district_never_broadens_search() -> None:
    criteria = SearchCriteria(
        user_id=1, city="Taraz", deal_type="sale", property_type="apartment",
        districts=["Center"],
    )
    assert not KrishaParser._matches_criteria(
        make_preview(district="Центральный район"), criteria
    )
    assert not KrishaParser._matches_criteria(
        make_preview(rooms=None, price_kzt=None), criteria
    )


@pytest.mark.asyncio
async def test_search_caps_detail_fetches_to_max_results() -> None:
    # one listing page advertising three listings, parser capped to 2 results
    cards = "".join(
        f'<div class="a-card">'
        f'<a class="a-card__title" href="/a/show/{i}">2-комнатная квартира · 60 м² · 3/9 этаж</a>'
        f'<div class="a-card__price">40 000 000 〒</div>'
        f"</div>"
        for i in range(1, 4)
    )
    listing_html = f"<html><body>{cards}</body></html>"
    detail_html = load_fixture("detail_123456789.html")

    redis = FakeRedis()
    parser = KrishaParser(
        redis_client=redis, min_delay_seconds=0, max_delay_seconds=0, max_results=2,
    )
    criteria = build_criteria(page_limit=1)
    listing_url = parser._build_listing_urls(criteria)[0]
    page_map = {
        listing_url: (200, listing_html),
        "https://krisha.kz/a/show/1": (200, detail_html),
        "https://krisha.kz/a/show/2": (200, detail_html),
        "https://krisha.kz/a/show/3": (200, detail_html),
    }

    apartments = await parser.search(FakeBrowserContext(page_map), criteria)

    assert len(apartments) == 2  # capped, third listing not fetched


@pytest.mark.asyncio
async def test_search_requires_detail_confirmation_for_requested_district() -> None:
    cards = "".join(
        f'<div class="a-card">'
        f'<a class="a-card__title" href="/a/show/{external_id}">'
        f"2-комнатная квартира · 60 м² · 3/9 этаж</a>"
        f'<div class="a-card__price">40 000 000 〒</div>'
        f"</div>"
        for external_id in ("1", "2")
    )
    listing_html = f"<html><body>{cards}</body></html>"
    bostandyk_detail = load_fixture("detail_123456789.html")
    medeu_detail = bostandyk_detail.replace(
        "Бостандыкский р-н",  # noqa: RUF001
        "Медеуский р-н",  # noqa: RUF001
    )
    redis = FakeRedis()
    parser = KrishaParser(
        redis_client=redis,
        min_delay_seconds=0,
        max_delay_seconds=0,
        max_results=1,
    )
    criteria = SearchCriteria(
        user_id=1,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        districts=["Bostandyk"],
        page_limit=1,
    )
    listing_url = parser._build_listing_urls(criteria)[0]
    context = FakeBrowserContext(
        {
            listing_url: (200, listing_html),
            "https://krisha.kz/a/show/1": (200, medeu_detail),
            "https://krisha.kz/a/show/2": (200, bostandyk_detail),
        }
    )

    apartments = await parser.search(context, criteria)

    assert [apartment.external_id for apartment in apartments] == ["2"]
    assert "krisha:seen:1:1" in redis.deleted_keys


def test_apartment_matches_criteria_rechecks_rooms_and_price_after_detail() -> None:
    # A sparse-preview promoted listing (no rooms/price on the card) passes the
    # preview filter, but the detail page reveals the real values; the post-detail
    # check must enforce rooms/price/area, not only district.
    criteria = SearchCriteria(
        user_id=1, city="Almaty", deal_type="sale", property_type="apartment",
        max_price_kzt=45_000_000, rooms=[2], districts=["Bostandyk"],
    )

    def apt(*, rooms: int | None, price: int, district: str) -> Apartment:
        return Apartment(
            external_id="x", url="https://krisha.kz/a/show/x", title="t",
            price_kzt=price, city="Almaty", district=district, rooms=rooms, photos=[],
        )

    # wrong rooms + over budget (the promoted 5-room/160M leak) -> dropped
    assert not KrishaParser._apartment_matches_criteria(
        apt(rooms=5, price=160_000_000, district="Бостандыкский р-н"), criteria  # noqa: RUF001
    )
    # right district but over budget -> dropped
    assert not KrishaParser._apartment_matches_criteria(
        apt(rooms=2, price=66_800_000, district="Бостандыкский р-н"), criteria  # noqa: RUF001
    )
    # fully matching -> kept
    assert KrishaParser._apartment_matches_criteria(
        apt(rooms=2, price=40_000_000, district="Бостандыкский р-н"), criteria  # noqa: RUF001
    )
    # rooms unknown even after detail, but price/district ok -> kept (None tolerated)
    assert KrishaParser._apartment_matches_criteria(
        apt(rooms=None, price=43_500_000, district="Бостандыкский р-н"), criteria  # noqa: RUF001
    )
    # in budget and right rooms but wrong district -> dropped
    assert not KrishaParser._apartment_matches_criteria(
        apt(rooms=2, price=40_000_000, district="Медеуский р-н"), criteria  # noqa: RUF001
    )


@pytest.mark.asyncio
async def test_search_ignores_recaptcha_legal_footer() -> None:
    # krisha adds this reCAPTCHA legal footer to every normal page; the parser
    # must not treat a content-rich result page as an anti-bot interstitial.
    recaptcha_footer = (
        '<p class="g-recaptcha-policy">'
        "Этот сайт защищён "
        "сервисом reCAPTCHA</p>"
    )
    listing_html = load_fixture("listing_page.html") + recaptcha_footer
    detail_html = load_fixture("detail_123456789.html") + recaptcha_footer

    redis = FakeRedis(seen_keys={"krisha:seen:1:987654321"})
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
    assert apartments[0].external_id == "123456789"


@pytest.mark.asyncio
async def test_search_releases_seen_reservation_when_detail_page_times_out() -> None:
    listing_html = load_fixture("listing_page.html")
    detail_html = load_fixture("detail_123456789.html")

    redis = FakeRedis()
    parser = KrishaParser(
        redis_client=redis,
        min_delay_seconds=0,
        max_delay_seconds=0,
        timeout_ms=10_000,
    )
    criteria = build_criteria(page_limit=1)
    listing_url = parser._build_listing_urls(criteria)[0]

    context = FakeBrowserContext(
        {
            listing_url: (200, listing_html),
            "https://krisha.kz/a/show/123456789": PlaywrightTimeoutError("detail timeout"),
            "https://krisha.kz/a/show/987654321": (200, detail_html),
        }
    )

    apartments = await parser.search(context, criteria)

    assert [apartment.external_id for apartment in apartments] == ["987654321"]
    assert redis.deleted_keys == ["krisha:seen:1:123456789"]
    assert redis._seen_keys == {"krisha:seen:1:987654321"}


@pytest.mark.asyncio
async def test_search_uses_other_listing_pages_when_one_page_times_out() -> None:
    listing_html = load_fixture("listing_page.html")
    detail_html = load_fixture("detail_123456789.html")

    redis = FakeRedis(seen_keys={"krisha:seen:1:987654321"})
    parser = KrishaParser(
        redis_client=redis,
        min_delay_seconds=0,
        max_delay_seconds=0,
        timeout_ms=10_000,
    )
    criteria = build_criteria(page_limit=2)
    first_page_url, second_page_url = parser._build_listing_urls(criteria)

    context = FakeBrowserContext(
        {
            first_page_url: PlaywrightTimeoutError("listing timeout"),
            second_page_url: (200, listing_html),
            "https://krisha.kz/a/show/123456789": (200, detail_html),
        }
    )

    apartments = await parser.search(context, criteria)

    assert [apartment.external_id for apartment in apartments] == ["123456789"]


@pytest.mark.asyncio
async def test_search_propagates_listing_timeout_when_no_pages_succeed() -> None:
    redis = FakeRedis()
    parser = KrishaParser(
        redis_client=redis,
        min_delay_seconds=0,
        max_delay_seconds=0,
        timeout_ms=10_000,
    )
    criteria = build_criteria(page_limit=1)
    listing_url = parser._build_listing_urls(criteria)[0]
    context = FakeBrowserContext(
        {listing_url: PlaywrightTimeoutError("listing timeout")}
    )

    with pytest.raises(PlaywrightTimeoutError):
        await parser.search(context, criteria)


def test_parse_listing_page_extracts_previews() -> None:
    listing_html = load_fixture("listing_page.html")
    parser = KrishaParser(redis_client=FakeRedis(), min_delay_seconds=0, max_delay_seconds=0)

    previews = parser.parse_listing_page(listing_html)

    assert len(previews) == 2
    assert previews[0].external_id == "123456789"
    assert previews[0].price_kzt == 35_000_000
    assert previews[1].external_id == "987654321"
    assert previews[1].rooms == 1


@pytest.mark.asyncio
async def test_check_health_passes_on_healthy_pages() -> None:
    listing_html = load_fixture("listing_page.html")
    detail_html = load_fixture("detail_123456789.html")
    parser = KrishaParser(redis_client=FakeRedis(), min_delay_seconds=0, max_delay_seconds=0)
    criteria = build_criteria(page_limit=1)
    listing_url = parser._build_listing_urls(criteria)[0]
    context = FakeBrowserContext(
        {
            listing_url: (200, listing_html),
            "https://krisha.kz/a/show/123456789": (200, detail_html),
        }
    )

    report = await parser.check_health(context, criteria=criteria)

    assert report.ok
    assert report.failures == []
    assert report.listing_count == 2
    assert report.previews_with_price >= 1
    assert report.previews_with_specs >= 1
    assert report.detail_checked


@pytest.mark.asyncio
async def test_check_health_flags_empty_listing_page() -> None:
    parser = KrishaParser(redis_client=FakeRedis(), min_delay_seconds=0, max_delay_seconds=0)
    criteria = build_criteria(page_limit=1)
    listing_url = parser._build_listing_urls(criteria)[0]
    # A page with no listing cards (e.g. krisha changed its markup) must surface
    # as a failure, never as a healthy empty result.
    context = FakeBrowserContext({listing_url: (200, "<html><body>no cards here</body></html>")})

    report = await parser.check_health(context, criteria=criteria)

    assert not report.ok
    assert report.listing_count == 0
    assert not report.detail_checked
    assert report.failures


@pytest.mark.asyncio
async def test_check_health_raises_on_blocked_page() -> None:
    captcha_html = load_fixture("captcha_page.html")
    parser = KrishaParser(redis_client=FakeRedis(), min_delay_seconds=0, max_delay_seconds=0)
    criteria = build_criteria(page_limit=1)
    listing_url = parser._build_listing_urls(criteria)[0]
    context = FakeBrowserContext({listing_url: (200, captcha_html)})

    # A block must propagate so the canary can report it distinctly from a markup change.
    with pytest.raises(AntiBotBlockedError):
        await parser.check_health(context, criteria=criteria)
