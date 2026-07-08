"""Refine feature: /refine, the guided menu, and its callback flows.

The typed-value FSM message handlers live in ``bot.routers.dialog`` (the last
included router) so that commands and callbacks registered here and in the
other feature routers always win over an in-progress typed-value state.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from agent.locations import LocationInputError
from bot.keyboards import (
    REFINE_BACK,
    REFINE_CALLBACK_DATA,
    REFINE_CITY_OTHER,
    REFINE_DISTRICT_CLEAR,
    REFINE_FIELD_PREFIX,
    REFINE_RUN,
    REFINE_SET_CITY_PREFIX,
    REFINE_SET_DEAL_PREFIX,
    REFINE_SET_DISTRICT_PREFIX,
    REFINE_SET_PERIOD_PREFIX,
    REFINE_TOGGLE_OWNER,
    build_refine_back_keyboard,
    build_refine_city_keyboard,
    build_refine_deal_keyboard,
    build_refine_district_keyboard,
    build_refine_rent_period_keyboard,
)
from bot.routers.shared import (
    NO_ACTIVE_CRITERIA_MESSAGE,
    REFINE_VALUE_PROMPTS,
    RouterHelpers,
    typing_action,
)
from bot.service import (
    ActiveCriteriaNotFoundError,
    CriteriaUnchangedError,
    SearchBotService,
    SearchExecutionError,
)
from bot.states import SearchDialogStates


def create_refine_router(service: SearchBotService, helpers: RouterHelpers) -> Router:
    router = Router(name="krisha-refine")

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
            # No text: open the guided menu (buttons for city/deal/district,
            # typed values for rooms/budget/area) instead of a blank free-text ask.
            await state.clear()
            await helpers.show_refine_menu(message, message.from_user.id, edit=False)
            return

        await message.answer("Уточняю критерии и запускаю поиск заново...")
        try:
            async with typing_action(message):
                result = await service.refine_search(
                    telegram_user_id=message.from_user.id,
                    username=message.from_user.username,
                    message=query,
                )
        except ActiveCriteriaNotFoundError:
            await message.answer(NO_ACTIVE_CRITERIA_MESSAGE)
            return
        except CriteriaUnchangedError:
            await message.answer(
                "Не удалось распознать изменение критериев. "  # noqa: RUF001
                "Напиши, например: «только 3 комнаты и до 35 млн»."
            )
            return
        except (LocationInputError, SearchExecutionError) as exc:
            await message.answer(exc.user_message)
            return

        await state.clear()
        await helpers.send_search_execution(message, state, result)

    @router.callback_query(F.data == REFINE_CALLBACK_DATA)
    async def handle_refine_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await state.clear()
        await helpers.show_refine_menu(callback.message, callback.from_user.id, edit=False)
        await callback.answer()

    @router.callback_query(F.data.startswith(REFINE_FIELD_PREFIX))
    async def handle_refine_field_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        field = (callback.data or "")[len(REFINE_FIELD_PREFIX):]
        message = callback.message
        if field == "city":
            with contextlib.suppress(Exception):
                await message.edit_text(
                    "🏙 Выберите город:", reply_markup=build_refine_city_keyboard()
                )
        elif field == "deal":
            with contextlib.suppress(Exception):
                await message.edit_text("🤝 Тип сделки:", reply_markup=build_refine_deal_keyboard())
        elif field == "period":
            with contextlib.suppress(Exception):
                await message.edit_text(
                    "⏱ Срок аренды:", reply_markup=build_refine_rent_period_keyboard()
                )
        elif field == "district":
            criteria = await service.get_active_criteria(telegram_user_id=callback.from_user.id)
            if criteria is None:
                await callback.answer("Сначала выполни поиск через /search.")
                return
            with contextlib.suppress(Exception):
                await message.edit_text(
                    "📍 Выберите район:",
                    reply_markup=build_refine_district_keyboard(criteria.city),
                )
        elif field in REFINE_VALUE_PROMPTS:
            await state.set_state(SearchDialogStates.waiting_for_refine_value)
            await state.update_data(refine_field=field)
            with contextlib.suppress(Exception):
                await message.edit_text(
                    REFINE_VALUE_PROMPTS[field],
                    reply_markup=build_refine_back_keyboard(),
                )
        await callback.answer()

    @router.callback_query(F.data.startswith(REFINE_SET_CITY_PREFIX))
    async def handle_refine_set_city_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        canonical = (callback.data or "")[len(REFINE_SET_CITY_PREFIX):]
        await service.set_active_city(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            city_text=canonical,
        )
        await state.clear()
        await helpers.show_refine_menu(callback.message, callback.from_user.id, edit=True)
        await callback.answer("Город обновлён")

    @router.callback_query(F.data == REFINE_CITY_OTHER)
    async def handle_refine_city_other_callback(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await state.set_state(SearchDialogStates.waiting_for_refine_value)
        await state.update_data(refine_field="city")
        with contextlib.suppress(Exception):
            await callback.message.edit_text(
                REFINE_VALUE_PROMPTS["city"], reply_markup=build_refine_back_keyboard()
            )
        await callback.answer()

    async def apply_deal_choice(
        callback: CallbackQuery,
        state: FSMContext,
        *,
        deal_type: str,
        rent_period: str | None,
    ) -> None:
        """Persist the deal choice; on a budget reset ask for a new budget."""
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        _, budget_reset = await service.set_active_deal_type(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            deal_type=deal_type,
            rent_period=rent_period,
        )
        if budget_reset:
            # The old budget belonged to the other terms — ask for a new one
            # right away instead of silently searching with no budget.
            await state.set_state(SearchDialogStates.waiting_for_refine_value)
            await state.update_data(refine_field="budget")
            labels = {
                "sale": ("покупки", "«до 45 млн»"),
                "rent:monthly": ("аренды помесячно", "«до 300 тыс»"),
                "rent:daily": ("аренды посуточно", "«до 20 тыс»"),
                "rent:hourly": ("аренды по часам", "«до 5 тыс»"),
            }
            key = deal_type if deal_type == "sale" else f"rent:{rent_period or 'monthly'}"
            deal_label, example = labels.get(key, ("аренды", "«до 300 тыс»"))
            with contextlib.suppress(Exception):
                await callback.message.edit_text(
                    f"Сделка обновлена, прежний бюджет сброшен.\n"
                    f"💰 Напишите бюджет для {deal_label} — например: {example}.",
                    reply_markup=build_refine_back_keyboard(),
                )
            await callback.answer("Сделка обновлена — укажите бюджет")
            return
        await state.clear()
        await helpers.show_refine_menu(callback.message, callback.from_user.id, edit=True)
        await callback.answer("Сделка обновлена")

    @router.callback_query(F.data.startswith(REFINE_SET_DEAL_PREFIX))
    async def handle_refine_set_deal_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        deal_type = (callback.data or "")[len(REFINE_SET_DEAL_PREFIX):]
        if deal_type == "rent":
            # Rent has a term (like krisha's selector): ask it as the second step
            # before persisting anything.
            with contextlib.suppress(Exception):
                await callback.message.edit_text(
                    "⏱ Срок аренды:", reply_markup=build_refine_rent_period_keyboard()
                )
            await callback.answer()
            return
        await apply_deal_choice(callback, state, deal_type=deal_type, rent_period=None)

    @router.callback_query(F.data.startswith(REFINE_SET_PERIOD_PREFIX))
    async def handle_refine_set_period_callback(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        rent_period = (callback.data or "")[len(REFINE_SET_PERIOD_PREFIX):]
        await apply_deal_choice(callback, state, deal_type="rent", rent_period=rent_period)

    @router.callback_query(F.data.startswith(REFINE_SET_DISTRICT_PREFIX))
    async def handle_refine_set_district_callback(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        value = (callback.data or "")[len(REFINE_SET_DISTRICT_PREFIX):]
        district = None if value == REFINE_DISTRICT_CLEAR else value
        await service.set_active_district(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            district=district,
        )
        await state.clear()
        await helpers.show_refine_menu(callback.message, callback.from_user.id, edit=True)
        await callback.answer("Район обновлён" if district else "Ищу по всему городу")

    @router.callback_query(F.data == REFINE_TOGGLE_OWNER)
    async def handle_refine_toggle_owner_callback(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        try:
            updated = await service.toggle_active_owner_only(
                telegram_user_id=callback.from_user.id,
                username=callback.from_user.username,
            )
        except ActiveCriteriaNotFoundError:
            await callback.answer("Сначала выполни поиск через /search.")
            return
        await state.clear()
        await helpers.show_refine_menu(callback.message, callback.from_user.id, edit=True)
        await callback.answer(
            "Только объявления от хозяев" if updated.owner_only else "Любые объявления"
        )

    @router.callback_query(F.data == REFINE_BACK)
    async def handle_refine_back_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await state.clear()
        await helpers.show_refine_menu(callback.message, callback.from_user.id, edit=True)
        await callback.answer()

    @router.callback_query(F.data == REFINE_RUN)
    async def handle_refine_run_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await callback.answer()
        await state.clear()
        target = callback.message
        await target.answer("Ищу по обновлённым критериям…")
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
        await helpers.send_search_execution(target, state, result)

    return router
