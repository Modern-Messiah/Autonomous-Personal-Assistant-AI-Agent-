"""Alembic environment configuration."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from urllib.parse import quote_plus

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from db.base import Base
from db.models import ApartmentRecord, SearchCriteriaRecord, SeenApartment, User

_ = (ApartmentRecord, SearchCriteriaRecord, SeenApartment, User)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> str:
    direct_url = os.getenv("DATABASE_URL")
    if direct_url:
        return direct_url

    db_host = os.getenv("DB__HOST")
    db_name = os.getenv("DB__NAME")
    db_user = os.getenv("DB__USER")
    db_password = os.getenv("DB__PASSWORD")
    db_port = os.getenv("DB__PORT", "5432")

    if db_host and db_name and db_user and db_password:
        safe_password = quote_plus(db_password)
        return f"postgresql+asyncpg://{db_user}:{safe_password}@{db_host}:{db_port}/{db_name}"

    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_database_url()

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
