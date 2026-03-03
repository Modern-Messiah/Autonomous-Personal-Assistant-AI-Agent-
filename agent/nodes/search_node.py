"""Search node for the LangGraph pipeline."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib import import_module
from typing import Protocol, TypedDict, cast

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from agent.tools import KrishaParser, build_redis_client
from agent.tools.krisha_parser import BrowserContextProtocol
from config.settings import get_settings


class SearchGraphState(TypedDict, total=False):
    """LangGraph state for apartment search."""

    criteria: SearchCriteria
    apartments: list[Apartment]
    enriched_apartments: list[EnrichedApartment]


class ParserProtocol(Protocol):
    """Parser contract required by search node."""

    async def search(
        self,
        context: BrowserContextProtocol,
        criteria: SearchCriteria,
    ) -> list[Apartment]: ...


class CloseableBrowserContextProtocol(BrowserContextProtocol, Protocol):
    """Browser context protocol with explicit close method."""

    async def close(self) -> None: ...


class SearchNode:
    """LangGraph node that executes the parser against Krisha."""

    def __init__(
        self,
        *,
        parser: ParserProtocol,
        context_factory: ContextFactoryProtocol,
    ) -> None:
        self._parser = parser
        self._context_factory = context_factory

    async def __call__(self, state: SearchGraphState) -> SearchGraphState:
        criteria = state["criteria"]
        async with self._context_factory() as context:
            apartments = await self._parser.search(context, criteria)
        return {"criteria": criteria, "apartments": apartments}


class ContextFactoryProtocol(Protocol):
    """Factory that returns async context manager with parser-ready browser context."""

    def __call__(self) -> AbstractAsyncContextManager[BrowserContextProtocol]: ...


def build_playwright_context_factory(parser: KrishaParser) -> ContextFactoryProtocol:
    """Create async context factory that yields Playwright browser context."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[BrowserContextProtocol]:
        playwright_module = import_module("playwright.async_api")
        async_playwright = playwright_module.async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await parser.create_browser_context(browser)
            closeable_context = cast(CloseableBrowserContextProtocol, context)
            try:
                yield closeable_context
            finally:
                await closeable_context.close()
                await browser.close()

    return cast(ContextFactoryProtocol, factory)


def create_default_search_node() -> SearchNode:
    """Create production-ready search node with parser + redis + playwright."""
    settings = get_settings()
    redis_client = build_redis_client(settings.redis.redis_url)
    parser = KrishaParser(
        redis_client=redis_client,
        min_delay_seconds=settings.parser.min_delay_seconds,
        max_delay_seconds=settings.parser.max_delay_seconds,
        timeout_ms=settings.parser.timeout_ms,
        dedup_ttl_seconds=settings.parser.dedup_ttl_seconds,
    )
    return SearchNode(parser=parser, context_factory=build_playwright_context_factory(parser))
