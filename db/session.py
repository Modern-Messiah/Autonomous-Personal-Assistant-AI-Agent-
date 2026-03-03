"""Async SQLAlchemy engine and session factories."""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config.settings import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return a cached async DB engine."""
    settings = get_settings()
    return create_async_engine(settings.db.sqlalchemy_url, pool_pre_ping=True)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Create session factory bound to the shared engine."""
    return async_sessionmaker(bind=get_engine(), expire_on_commit=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield DB session for request handlers and background tasks."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session

