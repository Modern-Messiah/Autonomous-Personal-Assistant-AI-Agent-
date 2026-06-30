"""Disposable PostgreSQL fixtures for repository integration tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

TABLES = (
    "apartment_feedback",
    "seen_apartments",
    "monitor_settings",
    "search_criteria",
    "apartments",
    "users",
)


@pytest_asyncio.fixture
async def integration_engine() -> AsyncIterator[AsyncEngine]:
    raw_url = os.getenv("TEST_DATABASE_URL")
    if raw_url is None:
        if os.getenv("CI"):
            pytest.fail("TEST_DATABASE_URL is required in CI")
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    url = make_url(raw_url)
    if not (url.database or "").endswith("_test"):
        pytest.fail("integration database name must end with _test")
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(
    integration_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with integration_engine.begin() as connection:
            await connection.execute(
                text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
            )
