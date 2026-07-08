"""Monitoring feature: /monitor status, on/off, interval."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.formatters import format_monitor_status
from bot.monitoring import parse_monitor_interval
from bot.service import SearchBotService


def create_monitor_router(service: SearchBotService) -> Router:
    router = Router(name="krisha-monitor")

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
