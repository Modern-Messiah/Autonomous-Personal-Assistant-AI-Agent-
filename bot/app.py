"""Telegram bot bootstrap."""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.router import create_bot_router
from bot.service import SearchBotService
from config.settings import get_settings
from db.session import get_session_factory


def create_bot() -> Bot:
    """Create configured aiogram Bot instance."""
    settings = get_settings()
    return Bot(
        token=settings.telegram.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(service: SearchBotService | None = None) -> Dispatcher:
    """Create dispatcher with project routes."""
    dispatcher = Dispatcher()
    active_service = service or SearchBotService(
        session_factory=get_session_factory(),
    )
    dispatcher.include_router(create_bot_router(active_service))
    return dispatcher


async def run_polling() -> None:
    """Start Telegram long polling."""
    bot = create_bot()
    dispatcher = create_dispatcher()
    await dispatcher.start_polling(bot)


def main() -> None:
    """CLI entrypoint for running the bot."""
    asyncio.run(run_polling())

