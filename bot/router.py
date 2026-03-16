"""aiogram router factory for bot commands."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.formatters import (
    format_criteria,
    format_monitor_status,
    format_saved_apartments,
    format_search_results,
    format_start_message,
)
from bot.keyboards import (
    LIST_CALLBACK_DATA,
    REFINE_CALLBACK_DATA,
    build_search_followup_keyboard,
)
from bot.monitoring import parse_monitor_interval
from bot.service import ActiveCriteriaNotFoundError, SearchBotService, SearchExecution
from bot.states import SearchDialogStates


def create_bot_router(service: SearchBotService) -> Router:
    """Create router with minimal command set for the bot."""
    router = Router(name="krisha-agent")

    async def send_search_execution(message: Message, result: SearchExecution) -> None:
        await message.answer(format_criteria(result.criteria))
        await message.answer(
            format_search_results(result.apartments),
            reply_markup=build_search_followup_keyboard(),
        )

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
        await send_search_execution(message, result)

    @router.message(Command("refine"))
    async def handle_refine_command(
        message: Message,
        command: CommandObject,
        state: FSMContext,
    ) -> None:
        if message.from_user is None:
            return

        query = (command.args or "").strip()
        if not query:
            criteria = await service.get_active_criteria(
                telegram_user_id=message.from_user.id,
            )
            if criteria is None:
                await message.answer(
                    "Активные критерии не найдены. Сначала выполни поиск через /search."
                )
                return
            await state.set_state(SearchDialogStates.waiting_for_refinement)
            await message.answer(
                "Напиши, что изменить в критериях, например:\n"
                "только 3 комнаты и до 35 млн\n\n"
                "Для выхода из режима уточнения используй /cancel."
            )
            return

        await message.answer("Уточняю критерии и запускаю поиск заново...")
        try:
            result = await service.refine_search(
                telegram_user_id=message.from_user.id,
                username=message.from_user.username,
                message=query,
            )
        except ActiveCriteriaNotFoundError:
            await message.answer(
                "Активные критерии не найдены. Сначала выполни поиск через /search."
            )
            return

        await state.clear()
        await send_search_execution(message, result)

    @router.message(Command("cancel"))
    async def handle_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Режим уточнения критериев отменен.")

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

    @router.callback_query(F.data == REFINE_CALLBACK_DATA)
    async def handle_refine_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return

        criteria = await service.get_active_criteria(
            telegram_user_id=callback.from_user.id,
        )
        if criteria is None:
            if callback.message is not None:
                await callback.message.answer(
                    "Активные критерии не найдены. Сначала выполни поиск через /search."
                )
            await callback.answer()
            return

        await state.set_state(SearchDialogStates.waiting_for_refinement)
        if callback.message is not None:
            await callback.message.answer(
                "Опиши уточнение свободным текстом. Пример:\n"
                "добавь район Медеу и бюджет до 50 млн"
            )
        await callback.answer()

    @router.callback_query(F.data == LIST_CALLBACK_DATA)
    async def handle_list_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        apartments = await service.get_saved_apartments(
            telegram_user_id=callback.from_user.id,
        )
        if callback.message is not None:
            await callback.message.answer(format_saved_apartments(apartments))
        await callback.answer()

    @router.message(SearchDialogStates.waiting_for_refinement)
    async def handle_refinement_message(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return

        text = (message.text or "").strip()
        if not text:
            await message.answer("Опиши уточнение текстом или используй /cancel.")
            return

        await message.answer("Уточняю критерии и запускаю поиск заново...")
        try:
            result = await service.refine_search(
                telegram_user_id=message.from_user.id,
                username=message.from_user.username,
                message=text,
            )
        except ActiveCriteriaNotFoundError:
            await state.clear()
            await message.answer(
                "Активные критерии не найдены. Сначала выполни поиск через /search."
            )
            return

        await state.clear()
        await send_search_execution(message, result)

    return router
