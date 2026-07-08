"""Search feature: /start, /help, /search, /criteria, /cancel, «Ещё варианты»."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from agent.locations import LocationInputError
from bot.formatters import format_criteria, format_start_message
from bot.keyboards import SEARCH_MORE_CALLBACK_DATA
from bot.routers.shared import NO_ACTIVE_CRITERIA_MESSAGE, RouterHelpers, typing_action
from bot.service import (
    ActiveCriteriaNotFoundError,
    SearchBotService,
    SearchExecutionError,
)


def create_search_router(service: SearchBotService, helpers: RouterHelpers) -> Router:
    router = Router(name="krisha-search")

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
            async with typing_action(message):
                result = await service.run_search(
                    telegram_user_id=message.from_user.id,
                    username=message.from_user.username,
                    query=query,
                )
        except (LocationInputError, SearchExecutionError) as exc:
            await message.answer(exc.user_message)
            return
        await helpers.send_search_execution(message, state, result)

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
            await message.answer(NO_ACTIVE_CRITERIA_MESSAGE)
            return
        await message.answer(format_criteria(criteria))

    @router.callback_query(F.data == SEARCH_MORE_CALLBACK_DATA)
    async def handle_search_more_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await callback.answer()  # ack the tap so the button stops spinning
        target = callback.message
        await target.answer("Ищу ещё варианты по тем же критериям…")
        try:
            async with typing_action(target):
                result = await service.rerun_active_search(
                    telegram_user_id=callback.from_user.id,
                    username=callback.from_user.username,
                )
        except ActiveCriteriaNotFoundError:
            await target.answer(NO_ACTIVE_CRITERIA_MESSAGE)
            return
        except (LocationInputError, SearchExecutionError) as exc:
            await target.answer(exc.user_message)
            return
        await helpers.send_search_execution(
            target,
            state,
            result,
            empty_message=(
                "🔍 Новых вариантов по текущим критериям пока нет — я показал всё, что нашлось.\n"
                "Уточни критерии кнопкой «Уточнить критерии» или загляни позже: "
                "объявления добавляются постоянно."
            ),
        )

    return router
