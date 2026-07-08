"""Feature routers assembled into the bot's single top-level router.

Inclusion order is semantic, not cosmetic: aiogram tries routers in order, so
the command/callback feature routers come first and ``dialog`` — whose handlers
match on FSM state or match any message (catch-all) — comes strictly last.
A command typed mid-flow (e.g. «/list» while a refine value is awaited) must
run the command, as it did when every handler lived in one module.
"""

from __future__ import annotations

from aiogram import Router

from bot.routers.dialog import create_dialog_router
from bot.routers.feedback import create_feedback_router
from bot.routers.monitor import create_monitor_router
from bot.routers.refine import create_refine_router
from bot.routers.search import create_search_router
from bot.routers.shared import RouterHelpers
from bot.service import SearchBotService

__all__ = ["create_bot_router"]


def create_bot_router(service: SearchBotService) -> Router:
    """Create the bot's router from the per-feature sub-routers."""
    router = Router(name="krisha-agent")
    helpers = RouterHelpers(service)
    router.include_router(create_search_router(service, helpers))
    router.include_router(create_refine_router(service, helpers))
    router.include_router(create_feedback_router(service, helpers))
    router.include_router(create_monitor_router(service))
    router.include_router(create_dialog_router(service, helpers))  # catch-all — LAST
    return router
