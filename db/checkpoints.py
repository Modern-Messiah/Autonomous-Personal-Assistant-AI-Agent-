"""Helpers for LangGraph checkpoint persistence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import import_module
from typing import Any

from config.settings import get_settings


def build_checkpoint_config(
    *,
    thread_id: str,
    checkpoint_ns: str = "",
    checkpoint_id: str | None = None,
) -> dict[str, dict[str, str]]:
    """Build LangGraph runnable config for checkpointed executions."""
    configurable: dict[str, str] = {
        "thread_id": thread_id,
        "checkpoint_ns": checkpoint_ns,
    }
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


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

