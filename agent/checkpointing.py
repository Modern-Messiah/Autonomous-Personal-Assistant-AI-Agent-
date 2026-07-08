"""Checkpointing contracts for the search graph — persistence-agnostic.

The graph knows it *can* be checkpointed, but not *where*: the concrete store
(Postgres in production) is injected by the composition root (bot/scheduler)
as a ``CheckpointerFactory``, so the ``agent`` package never imports ``db``.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol


class CheckpointerFactory(Protocol):
    """Opens a LangGraph checkpointer; the backing store is the caller's choice."""

    def __call__(self, *, setup: bool = True) -> AbstractAsyncContextManager[Any]: ...


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
