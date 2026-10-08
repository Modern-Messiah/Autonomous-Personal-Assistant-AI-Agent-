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

# Setup DDL is owned by Alembic migration 202610070008 (langgraph checkpoint
# tables), so the runtime role needs no DDL privileges. ``setup=True`` remains
# available for one-off bootstrap flows; it is idempotent and runs at most once
# per process (two searches racing the first call may both run it — harmless).
_checkpoint_setup_done: bool = False


@asynccontextmanager
async def get_async_postgres_checkpointer(*, setup: bool = False) -> AsyncIterator[Any]:
    """Yield official LangGraph Postgres saver bound to current DB settings.

    The checkpoint schema is created by ``alembic upgrade head`` (migration
    202610070008 mirrors ``AsyncPostgresSaver.setup()`` exactly, bookkeeping
    included), so DDL at runtime is off by default. Pass ``setup=True`` only for
    explicit one-off bootstrap outside the migration path.
    """
    checkpoint_module = import_module("langgraph.checkpoint.postgres.aio")
    async_postgres_saver = checkpoint_module.AsyncPostgresSaver

    global _checkpoint_setup_done
    settings = get_settings()
    async with async_postgres_saver.from_conn_string(settings.db.psycopg_url) as saver:
        if setup and not _checkpoint_setup_done:
            await saver.setup()
            _checkpoint_setup_done = True
        yield saver
