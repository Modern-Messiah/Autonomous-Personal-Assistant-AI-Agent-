"""Stateful free-text flows: typed refine values, refinement, feedback, catch-all.

This router is included LAST on purpose: every message handler here matches on
FSM state (or matches anything, for the catch-all), so commands and callbacks
registered in the feature routers must be tried first — «/list» typed while the
bot waits for a refine value must still run the /list command, exactly as when
all handlers lived in one module.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.dialog_agent import DialogAgent
from bot.routers.shared import NO_ACTIVE_CRITERIA_MESSAGE, RouterHelpers, typing_action
from bot.service import (
    ActiveCriteriaNotFoundError,
    CriteriaUnchangedError,
    SearchBotService,
    SearchExecutionError,
)
from bot.states import SearchDialogStates


def create_dialog_router(service: SearchBotService, helpers: RouterHelpers) -> Router:
    router = Router(name="krisha-dialog")

    def create_dialog_agent() -> DialogAgent:
        return DialogAgent(service)

    @router.message(SearchDialogStates.waiting_for_refine_value)
    async def handle_refine_value_message(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer("Введите значение или нажмите «← Назад».")
            return
        data = await state.get_data()
        field = data.get("refine_field")
        try:
            if field == "city":
                _, resolved = await service.set_active_city(
                    telegram_user_id=message.from_user.id,
                    username=message.from_user.username,
                    city_text=text,
                )
                if not resolved:
                    await message.answer(
                        "Город не распознан. Попробуйте ещё раз, например: Павлодар."
                    )
                    return  # stay in the value state so the user can retry
            else:
                await service.apply_refinement_value(
                    telegram_user_id=message.from_user.id,
                    username=message.from_user.username,
                    message=text,
                )
        except ActiveCriteriaNotFoundError:
            await state.clear()
            await message.answer(NO_ACTIVE_CRITERIA_MESSAGE)
            return
        await state.clear()
        await helpers.show_refine_menu(message, message.from_user.id, edit=False)

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
            async with typing_action(message):
                result = await service.refine_search(
                    telegram_user_id=message.from_user.id,
                    username=message.from_user.username,
                    message=text,
                )
        except ActiveCriteriaNotFoundError:
            await state.clear()
            await message.answer(NO_ACTIVE_CRITERIA_MESSAGE)
            return
        except CriteriaUnchangedError:
            await message.answer(
                "Не удалось распознать изменение критериев. "  # noqa: RUF001
                "Попробуй указать комнаты, бюджет, район, площадь или город."
            )
            return
        except SearchExecutionError as exc:
            await message.answer(exc.user_message)
            return

        await state.clear()
        await helpers.send_search_execution(message, state, result)

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

        async with typing_action(message):
            result = await create_dialog_agent().handle_message(
                telegram_user_id=message.from_user.id,
                username=message.from_user.username,
                message=text,
                on_search_start=notify_search_start,
            )
        await helpers.send_dialog_turn(message, state, result)

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

        async with typing_action(message):
            result = await create_dialog_agent().handle_message(
                telegram_user_id=message.from_user.id,
                username=message.from_user.username,
                message=text,
                on_search_start=notify_search_start,
            )
        await helpers.send_dialog_turn(message, state, result)

    return router
