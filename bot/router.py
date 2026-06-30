"""aiogram router factory for bot commands."""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.dialog_agent import DialogAgent, DialogTurnResult
from bot.formatters import (
    DEFAULT_SEARCH_RESULTS_LIMIT,
    format_apartment_card,
    format_criteria,
    format_monitor_status,
    format_search_results,
    format_start_message,
)
from bot.keyboards import (
    APT_REJECT_PREFIX,
    APT_SAVE_PREFIX,
    DELETE_SAVED_PREFIX,
    LIST_CALLBACK_DATA,
    REFINE_CALLBACK_DATA,
    build_apartment_actions_keyboard,
    build_saved_item_keyboard,
    build_search_followup_keyboard,
)
from bot.monitoring import parse_monitor_interval
from bot.service import (
    ActiveCriteriaNotFoundError,
    SearchBotService,
    SearchExecution,
    SearchExecutionError,
)
from bot.states import SearchDialogStates


def create_bot_router(service: SearchBotService) -> Router:
    """Create router with minimal command set for the bot."""
    router = Router(name="krisha-agent")

    def create_dialog_agent() -> DialogAgent:
        return DialogAgent(service)

    async def send_search_execution(
        message: Message,
        state: FSMContext,
        result: SearchExecution,
    ) -> None:
        presented_apartments = result.apartments[:DEFAULT_SEARCH_RESULTS_LIMIT]
        await message.answer(format_criteria(result.criteria))
        if not presented_apartments:
            await state.clear()
            await message.answer(format_search_results([]))
            return

        for index, apartment in enumerate(presented_apartments, start=1):
            caption = format_apartment_card(apartment, index=index)
            keyboard = build_apartment_actions_keyboard(apartment.apartment.external_id)
            photo = apartment.apartment.photos[0] if apartment.apartment.photos else None
            if photo is not None:
                try:
                    await message.answer_photo(photo=photo, caption=caption, reply_markup=keyboard)
                    continue
                except Exception:
                    # Telegram may reject a photo URL; fall back to a text card.
                    pass
            await message.answer(caption, reply_markup=keyboard)

        await message.answer(
            "Что делаем дальше?",
            reply_markup=build_search_followup_keyboard(),
        )
        await state.set_state(SearchDialogStates.waiting_for_feedback)

    async def send_saved_list(target: Message, telegram_user_id: int) -> None:
        apartments = await service.get_saved_apartments(telegram_user_id=telegram_user_id)
        if not apartments:
            await target.answer("Сохраненных квартир пока нет.")
            return
        await target.answer("Сохраненные квартиры:")
        for index, item in enumerate(apartments, start=1):
            caption = format_apartment_card(item, index=index)
            keyboard = build_saved_item_keyboard(item.apartment.external_id)
            photo = item.apartment.photos[0] if item.apartment.photos else None
            if photo is not None:
                try:
                    await target.answer_photo(photo=photo, caption=caption, reply_markup=keyboard)
                    continue
                except Exception:
                    pass
            await target.answer(caption, reply_markup=keyboard)

    async def send_dialog_turn(
        message: Message,
        state: FSMContext,
        result: DialogTurnResult,
    ) -> None:
        for item in result.messages:
            await message.answer(item)

        if result.search_execution is not None:
            await send_search_execution(message, state, result.search_execution)
            return

        if result.next_state == "clear":
            await state.clear()
            return

        await state.set_state(SearchDialogStates.waiting_for_feedback)

    @router.message(CommandStart())
    async def handle_start(message: Message) -> None:
        if message.from_user is None:
            return
        await service.register_user(
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
        )
        await message.answer(format_start_message())

    @router.message(Command("help"))
    async def handle_help(message: Message) -> None:
        await message.answer(format_start_message())

    @router.message(Command("search"))
    async def handle_search(
        message: Message,
        command: CommandObject,
        state: FSMContext,
    ) -> None:
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
        try:
            result = await service.run_search(
                telegram_user_id=message.from_user.id,
                username=message.from_user.username,
                query=query,
            )
        except SearchExecutionError as exc:
            await message.answer(exc.user_message)
            return
        await send_search_execution(message, state, result)

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
        except SearchExecutionError as exc:
            await message.answer(exc.user_message)
            return

        await state.clear()
        await send_search_execution(message, state, result)

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
        await send_saved_list(message, message.from_user.id)

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

    @router.callback_query(F.data.startswith(APT_SAVE_PREFIX))
    async def handle_apartment_save_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer()
            return
        external_id = callback.data[len(APT_SAVE_PREFIX):]
        saved = await service.save_apartment(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            external_id=external_id,
        )
        if saved and isinstance(callback.message, Message):
            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("💾 Сохранено — доступно в /list" if saved else "Квартира не найдена")

    @router.callback_query(F.data.startswith(APT_REJECT_PREFIX))
    async def handle_apartment_reject_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer()
            return
        external_id = callback.data[len(APT_REJECT_PREFIX):]
        rejected = await service.reject_apartment(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            external_id=external_id,
        )
        if rejected and isinstance(callback.message, Message):
            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer(
            "🚫 Отклонено — больше не покажу" if rejected else "Квартира не найдена"
        )

    @router.callback_query(F.data == LIST_CALLBACK_DATA)
    async def handle_list_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await send_saved_list(callback.message, callback.from_user.id)
        await callback.answer()

    @router.callback_query(F.data.startswith(DELETE_SAVED_PREFIX))
    async def handle_delete_saved_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer()
            return
        external_id = callback.data[len(DELETE_SAVED_PREFIX):]
        removed = await service.delete_saved_apartment(
            telegram_user_id=callback.from_user.id,
            external_id=external_id,
        )
        # Remove the card (works for both photo and text messages, unlike
        # edit_text which fails on a photo message).
        if removed and isinstance(callback.message, Message):
            with contextlib.suppress(Exception):
                await callback.message.delete()
        await callback.answer("🗑 Удалено из сохранённых" if removed else "Уже удалено")

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
        except SearchExecutionError as exc:
            await message.answer(exc.user_message)
            return

        await state.clear()
        await send_search_execution(message, state, result)

    @router.message(SearchDialogStates.waiting_for_feedback)
    async def handle_feedback_message(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer("Напиши новый запрос, уточнение или используй /cancel.")
            return

        async def notify_search_start() -> None:
            await message.answer("Ищу варианты по заданным критериям...")

        result = await create_dialog_agent().handle_message(
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            message=text,
            on_search_start=notify_search_start,
        )
        await send_dialog_turn(message, state, result)

    @router.message()
    async def handle_dialog_message(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer("Поддерживаются текстовые команды и обычные текстовые запросы.")
            return

        async def notify_search_start() -> None:
            await message.answer("Ищу варианты по заданным критериям...")

        result = await create_dialog_agent().handle_message(
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            message=text,
            on_search_start=notify_search_start,
        )
        await send_dialog_turn(message, state, result)

    return router
