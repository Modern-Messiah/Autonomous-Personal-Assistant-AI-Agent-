"""aiogram router factory for bot commands."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from bot.formatters import (
    format_criteria,
    format_monitor_status,
    format_saved_apartments,
    format_search_results,
    format_start_message,
)
from bot.monitoring import parse_monitor_interval
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

    @router.message(Command("list"))
    async def handle_list(message: Message) -> None:
        if message.from_user is None:
            return
        apartments = await service.get_saved_apartments(
            telegram_user_id=message.from_user.id,
        )
        await message.answer(format_saved_apartments(apartments))

    @router.message(Command("monitor"))
    async def handle_monitor(message: Message, command: CommandObject) -> None:
        if message.from_user is None:
            return

        raw_args = (command.args or "").strip()
        if not raw_args:
            status = await service.get_monitor_status(
                telegram_user_id=message.from_user.id,
            )
            if status is None:
                status = service.get_default_monitor_status()
            await message.answer(format_monitor_status(status))
            return

        command_name, _, rest = raw_args.partition(" ")
        action = command_name.lower()

        if action == "on":
            status = await service.set_monitor_enabled(
                telegram_user_id=message.from_user.id,
                username=message.from_user.username,
                enabled=True,
            )
            await message.answer("Мониторинг включен.\n\n" + format_monitor_status(status))
            return

        if action == "off":
            status = await service.set_monitor_enabled(
                telegram_user_id=message.from_user.id,
                username=message.from_user.username,
                enabled=False,
            )
            await message.answer("Мониторинг выключен.\n\n" + format_monitor_status(status))
            return

        if action == "interval":
            interval_input = rest.strip()
            if not interval_input:
                await message.answer(
                    "Укажи интервал после команды, например: /monitor interval 6h"
                )
                return
            try:
                interval_minutes = parse_monitor_interval(interval_input)
            except ValueError as exc:
                await message.answer(
                    "Некорректный интервал. Используй формат вроде 30m, 6h или 1d.\n"
                    f"Детали: {exc}"
                )
                return

            status = await service.set_monitor_interval(
                telegram_user_id=message.from_user.id,
                username=message.from_user.username,
                interval_minutes=interval_minutes,
            )
            await message.answer(
                "Интервал мониторинга обновлен.\n\n" + format_monitor_status(status)
            )
            return

        await message.answer(
            "Поддерживаются команды: /monitor, /monitor on, /monitor off, "
            "/monitor interval 6h"
        )

    return router
