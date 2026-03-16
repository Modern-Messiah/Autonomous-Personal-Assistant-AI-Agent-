"""aiogram router factory for bot commands."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from bot.formatters import format_criteria, format_search_results, format_start_message
from bot.service import SearchBotService


def create_bot_router(service: SearchBotService) -> Router:
    """Create router with minimal command set for the bot."""
    router = Router(name="krisha-agent")

    @router.message(CommandStart())
    async def handle_start(message: Message) -> None:
        if message.from_user is None:
            return
        await service.register_user(
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
        )
        await message.answer(format_start_message())

    @router.message(Command("search"))
    async def handle_search(message: Message, command: CommandObject) -> None:
        if message.from_user is None:
            return
        query = (command.args or "").strip()
        if not query:
            await message.answer(
                "Добавь поисковый запрос после команды, например:\n"
                "/search 2-комнатная квартира в Алматы до 45 млн"
            )
            return

        await message.answer("Ищу варианты по заданным критериям...")
        result = await service.run_search(
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            query=query,
        )
        await message.answer(format_criteria(result.criteria))
        await message.answer(format_search_results(result.apartments))

    @router.message(Command("criteria"))
    async def handle_criteria(message: Message) -> None:
        if message.from_user is None:
            return
        criteria = await service.get_active_criteria(
            telegram_user_id=message.from_user.id,
        )
        if criteria is None:
            await message.answer(
                "Активные критерии не найдены. Сначала выполни поиск через /search."
            )
            return
        await message.answer(format_criteria(criteria))

    return router

