"""Telegram bot bootstrap."""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from agent.tools import NotionClient
from bot.router import create_bot_router
from bot.service import SearchBotService
from config.observability import configure_observability
from config.settings import get_settings
from db.session import get_session_factory

BOT_COMMANDS = [
    BotCommand(command="search", description="Поиск квартир"),
    BotCommand(command="criteria", description="Текущие критерии"),
    BotCommand(command="list", description="Сохранённые квартиры"),
    BotCommand(command="trash", description="Корзина — вернуть удалённые"),
    BotCommand(command="foryou", description="Подборка под ваш вкус"),
    BotCommand(command="refine", description="Уточнить критерии"),
    BotCommand(command="cancel", description="Отменить уточнение"),
    BotCommand(command="monitor", description="Мониторинг новых объявлений"),
    BotCommand(command="help", description="Помощь по командам"),
    BotCommand(command="start", description="Запуск и помощь"),
]


def create_bot() -> Bot:
    """Create configured aiogram Bot instance."""
    settings = get_settings()
    return Bot(token=settings.telegram.bot_token.get_secret_value())


def create_dispatcher(service: SearchBotService | None = None) -> Dispatcher:
    """Create dispatcher with project routes."""
    dispatcher = Dispatcher()
    active_service = service or create_search_service()
    dispatcher.include_router(create_bot_router(active_service))
    return dispatcher


def create_search_service() -> SearchBotService:
    """Create bot service with optional Notion sync integration."""
    settings = get_settings()
    notion_sync = None
    if settings.notion.enabled:
        api_token = settings.notion.api_token
        database_id = settings.notion.database_id
        if api_token is None or database_id is None:
            msg = "Notion sync is enabled but credentials are incomplete"
            raise ValueError(msg)
        notion_sync = NotionClient(
            api_token=api_token.get_secret_value(),
            database_id=database_id,
            timeout_seconds=settings.notion.timeout_seconds,
        )
    return SearchBotService(
        session_factory=get_session_factory(),
        notion_sync=notion_sync,
    )


async def run_polling() -> None:
    """Start Telegram long polling."""
    bot = create_bot()
    dispatcher = create_dispatcher()
    # Register the command menu so commands autocomplete under "/" in Telegram.
    await bot.set_my_commands(BOT_COMMANDS)
    await dispatcher.start_polling(bot)


def main() -> None:
    """CLI entrypoint for running the bot."""
    configure_observability()
    asyncio.run(run_polling())
