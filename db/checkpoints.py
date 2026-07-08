"""Postgres implementation of the search graph's checkpointer contract.

The pure config builder lives in ``agent.checkpointing`` (the graph side of the
contract) and is re-exported here so existing ``from db import
build_checkpoint_config`` imports keep working. ``get_async_postgres_checkpointer``
satisfies ``agent.checkpointing.CheckpointerFactory`` and is injected into the
graph by the composition roots (bot/scheduler) — the agent package itself never
imports this module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import import_module
from typing import Any

from agent.checkpointing import build_checkpoint_config
from config.settings import get_settings

__all__ = ["build_checkpoint_config", "get_async_postgres_checkpointer"]


@asynccontextmanager
async def get_async_postgres_checkpointer(*, setup: bool = True) -> AsyncIterator[Any]:
    """Yield official LangGraph Postgres saver bound to current DB settings."""
    checkpoint_module = import_module("langgraph.checkpoint.postgres.aio")
    async_postgres_saver = checkpoint_module.AsyncPostgresSaver

    settings = get_settings()
    async with async_postgres_saver.from_conn_string(settings.db.psycopg_url) as saver:
        if setup:
            await saver.setup()
        yield saver
