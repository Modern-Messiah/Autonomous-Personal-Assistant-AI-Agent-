"""aiogram router factory for bot commands."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from agent.locations import LocationInputError
from agent.models.enriched import EnrichedApartment
from bot.card_sender import send_apartment_card
from bot.dialog_agent import DialogAgent, DialogTurnResult
from bot.formatters import (
    DEFAULT_SEARCH_RESULTS_LIMIT,
    clean_listing_url,
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
    PURGE_TRASH_PREFIX,
    REFINE_BACK,
    REFINE_CALLBACK_DATA,
    REFINE_CITY_OTHER,
    REFINE_DISTRICT_CLEAR,
    REFINE_FIELD_PREFIX,
    REFINE_RUN,
    REFINE_SET_CITY_PREFIX,
    REFINE_SET_DEAL_PREFIX,
    REFINE_SET_DISTRICT_PREFIX,
    RESTORE_TRASH_PREFIX,
    SEARCH_MORE_CALLBACK_DATA,
    build_apartment_actions_keyboard,
    build_refine_back_keyboard,
    build_refine_city_keyboard,
    build_refine_deal_keyboard,
    build_refine_district_keyboard,
    build_refine_menu_keyboard,
    build_saved_item_keyboard,
    build_search_followup_keyboard,
    build_trashed_item_keyboard,
)
from bot.monitoring import parse_monitor_interval
from bot.service import (
    ActiveCriteriaNotFoundError,
    CriteriaUnchangedError,
    NoPreferencesError,
    RecommendationResult,
    SearchBotService,
    SearchExecution,
    SearchExecutionError,
)
from bot.states import SearchDialogStates

# Prompts for the typed fields of the guided refine menu, keyed by field name
# stored in FSM data under "refine_field".
REFINE_VALUE_PROMPTS = {
    "rooms": "🚪 Напишите число комнат — например: 2, 2-3 или «двухкомнатная».",
    "budget": "💰 Напишите бюджет — например: «до 45 млн» или «от 20 до 45 млн».",
    "area": "📐 Напишите площадь — например: «от 50 м²» или «от 50 до 80 м²».",
    "city": "🏙 Напишите город — например: Павлодар.",
}
REFINE_MENU_HINT = "🔧 Что изменить? Выбирай кнопками или введи значение, потом жми «Искать»."


def _batch_avg_price_per_m2(apartments: list[EnrichedApartment]) -> float | None:
    """Average ₸/м² across the batch, for the per-card price comparison.

    Needs at least two listings with known price and area — otherwise a
    comparison against "the batch" is meaningless and the line is omitted.
    """
    values = [
        item.apartment.price_kzt / item.apartment.area_m2
        for item in apartments
        if item.apartment.area_m2 and item.apartment.area_m2 > 0
    ]
    if len(values) < 2:
        return None
    return sum(values) / len(values)


def create_bot_router(service: SearchBotService) -> Router:
    """Create router with minimal command set for the bot."""
    router = Router(name="krisha-agent")

    def create_dialog_agent() -> DialogAgent:
        return DialogAgent(service)

    @contextlib.asynccontextmanager
    async def typing(message: Message) -> AsyncIterator[None]:
        """Keep a live 'печатает…' indicator alive while a slow handler runs."""
        if message.bot is None:
            yield
            return
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            yield

    async def show_refine_menu(
        target: Message,
        telegram_user_id: int,
        *,
        edit: bool,
    ) -> bool:
        """Render the guided-refine menu (current criteria + field buttons).

        Edits the message in place when ``edit`` is True (button navigation),
        otherwise sends a new message. Returns False if there are no active
        criteria yet.
        """
        criteria = await service.get_active_criteria(telegram_user_id=telegram_user_id)
        if criteria is None:
            await target.answer(
                "Активные критерии не найдены. Сначала выполни поиск через /search."
            )
            return False
        text = f"{REFINE_MENU_HINT}\n\n{format_criteria(criteria)}"
        markup = build_refine_menu_keyboard(criteria.city)
        if edit:
            with contextlib.suppress(Exception):
                await target.edit_text(text, reply_markup=markup)
                return True
        await target.answer(text, reply_markup=markup)
        return True

    async def send_search_execution(
        message: Message,
        state: FSMContext,
        result: SearchExecution,
        *,
        empty_message: str | None = None,
    ) -> None:
        presented_apartments = result.apartments[:DEFAULT_SEARCH_RESULTS_LIMIT]
        for notice in result.notices:
            await message.answer(notice)
        await message.answer(format_criteria(result.criteria))
        if not presented_apartments:
            await state.clear()
            await message.answer(empty_message or format_search_results([]))
            return

        avg_price_per_m2 = _batch_avg_price_per_m2(presented_apartments)
        for index, apartment in enumerate(presented_apartments, start=1):
            keyboard = build_apartment_actions_keyboard(
                apartment.apartment.external_id,
                clean_listing_url(apartment.apartment.url),
            )
            await send_apartment_card(
                apartment,
                index=index,
                reply_markup=keyboard,
                send_text=message.answer,
                send_photo=message.answer_photo,
                avg_price_per_m2=avg_price_per_m2,
            )

        await message.answer(
            "Что делаем дальше?",
            reply_markup=build_search_followup_keyboard(),
        )
        await state.set_state(SearchDialogStates.waiting_for_feedback)

    async def send_saved_list(target: Message, telegram_user_id: int) -> None:
        apartments = await service.get_saved_apartments(telegram_user_id=telegram_user_id)
        if not apartments:
            await target.answer(
                "💾 Сохранённых квартир пока нет.\n"
                "Запустите поиск через /search и нажмите «💾 Сохранить» на карточке."
            )
            return
        total = await service.count_saved_apartments(telegram_user_id=telegram_user_id)
        shown = len(apartments)
        if total > shown:
            header = f"💾 Сохранённые квартиры: показаны последние {shown} из {total}"
        else:
            header = f"💾 Сохранённые квартиры ({total}):"
        await target.answer(header)
        for index, item in enumerate(apartments, start=1):
            keyboard = build_saved_item_keyboard(
                item.apartment.external_id,
                clean_listing_url(item.apartment.url),
            )
            await send_apartment_card(
                item,
                index=index,
                reply_markup=keyboard,
                send_text=target.answer,
                send_photo=target.answer_photo,
            )

    async def send_trash_list(target: Message, telegram_user_id: int) -> None:
        apartments = await service.get_trashed_apartments(telegram_user_id=telegram_user_id)
        if not apartments:
            await target.answer(
                "🗑 Корзина пуста. Сюда попадают отклонённые (🚫) квартиры и "
                "удалённые из /list — любую можно вернуть."
            )
            return
        await target.answer(
            f"🗑 Корзина ({len(apartments)}) — отклонённые и удалённые, "
            "можно вернуть кнопкой ♻️:"
        )
        for index, item in enumerate(apartments, start=1):
            keyboard = build_trashed_item_keyboard(
                item.apartment.external_id,
                clean_listing_url(item.apartment.url),
            )
            await send_apartment_card(
                item,
                index=index,
                reply_markup=keyboard,
                send_text=target.answer,
                send_photo=target.answer_photo,
            )

    async def send_dialog_turn(
        message: Message,
        state: FSMContext,
        result: DialogTurnResult,
    ) -> None:
        for item in result.messages:
            await message.answer(item)

        if result.show_saved and message.from_user is not None:
            await send_saved_list(message, message.from_user.id)

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
            async with typing(message):
                result = await service.run_search(
                    telegram_user_id=message.from_user.id,
                    username=message.from_user.username,
                    query=query,
                )
        except (LocationInputError, SearchExecutionError) as exc:
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
            # No text: open the guided menu (buttons for city/deal/district,
            # typed values for rooms/budget/area) instead of a blank free-text ask.
            await state.clear()
            await show_refine_menu(message, message.from_user.id, edit=False)
            return

        await message.answer("Уточняю критерии и запускаю поиск заново...")
        try:
            async with typing(message):
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

    @router.message(Command("trash"))
    async def handle_trash(message: Message) -> None:
        if message.from_user is None:
            return
        await send_trash_list(message, message.from_user.id)

    async def send_recommendations(target: Message, result: RecommendationResult) -> None:
        if not result.recommendations:
            await target.answer(
                "Сейчас нет свежих вариантов под ваш вкус. "
                "Загляните позже или уточните поиск через /search."
            )
            return
        await target.answer(
            f"⭐ Подобрал под ваши предпочтения ({len(result.recommendations)}):"
        )
        avg_price_per_m2 = _batch_avg_price_per_m2(
            [rec.apartment for rec in result.recommendations]
        )
        for index, rec in enumerate(result.recommendations, start=1):
            keyboard = build_apartment_actions_keyboard(
                rec.apartment.apartment.external_id,
                clean_listing_url(rec.apartment.apartment.url),
            )
            # rec.reasons stay internal (they drive the ranking); the card is
            # already dense and the "⭐ Почему вам" line only repeated it.
            await send_apartment_card(
                rec.apartment,
                index=index,
                reply_markup=keyboard,
                send_text=target.answer,
                send_photo=target.answer_photo,
                avg_price_per_m2=avg_price_per_m2,
            )

    @router.message(Command("foryou"))
    async def handle_foryou(message: Message) -> None:
        if message.from_user is None:
            return
        # Recommendation runs a live search (~30s); acknowledge immediately and
        # keep a typing indicator alive so the bot doesn't look frozen.
        await message.answer("⭐ Подбираю под ваш вкус — это займёт около минуты…")
        try:
            async with typing(message):
                result = await service.recommend(
                    telegram_user_id=message.from_user.id,
                    username=message.from_user.username,
                )
        except ActiveCriteriaNotFoundError:
            await message.answer(
                "Сначала задайте поиск через /search — потом /foryou подберёт под ваш вкус."
            )
            return
        except NoPreferencesError:
            await message.answer(
                "Пока нечему учиться. Сохраните несколько квартир кнопкой 💾 на карточке, "
                "и /foryou начнёт подбирать похожие."
            )
            return
        except (LocationInputError, SearchExecutionError) as exc:
            await message.answer(exc.user_message)
            return
        await send_recommendations(message, result)

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

    @router.callback_query(F.data == SEARCH_MORE_CALLBACK_DATA)
    async def handle_search_more_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await callback.answer()  # ack the tap so the button stops spinning
        target = callback.message
        await target.answer("Ищу ещё варианты по тем же критериям…")
        try:
            async with typing(target):
                result = await service.rerun_active_search(
                    telegram_user_id=callback.from_user.id,
                    username=callback.from_user.username,
                )
        except ActiveCriteriaNotFoundError:
            await target.answer(
                "Активные критерии не найдены. Сначала выполни поиск через /search."
            )
            return
        except (LocationInputError, SearchExecutionError) as exc:
            await target.answer(exc.user_message)
            return
        await send_search_execution(
            target,
            state,
            result,
            empty_message=(
                "🔍 Новых вариантов по текущим критериям пока нет — я показал всё, что нашлось.\n"
                "Уточни критерии кнопкой «Уточнить критерии» или загляни позже: "
                "объявления добавляются постоянно."
            ),
        )

    @router.callback_query(F.data == REFINE_CALLBACK_DATA)
    async def handle_refine_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await state.clear()
        await show_refine_menu(callback.message, callback.from_user.id, edit=False)
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
        await show_refine_menu(callback.message, callback.from_user.id, edit=True)
        await callback.answer("Город обновлён")

    @router.callback_query(F.data == REFINE_CITY_OTHER)
    async def handle_refine_city_other_callback(callback: CallbackQuery, state: FSMContext) -> None:
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

    @router.callback_query(F.data.startswith(REFINE_SET_DEAL_PREFIX))
    async def handle_refine_set_deal_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        deal_type = (callback.data or "")[len(REFINE_SET_DEAL_PREFIX):]
        _, budget_reset = await service.set_active_deal_type(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            deal_type=deal_type,
        )
        if budget_reset:
            # The old budget belonged to the other deal type — ask for a new one
            # right away instead of silently searching with no budget.
            await state.set_state(SearchDialogStates.waiting_for_refine_value)
            await state.update_data(refine_field="budget")
            deal_label = "аренды" if deal_type == "rent" else "покупки"
            with contextlib.suppress(Exception):
                await callback.message.edit_text(
                    f"Сделка обновлена, прежний бюджет сброшен.\n"
                    f"💰 Напишите бюджет для {deal_label} — например: "
                    + ("«до 300 тыс»." if deal_type == "rent" else "«до 45 млн»."),
                    reply_markup=build_refine_back_keyboard(),
                )
            await callback.answer("Сделка обновлена — укажите бюджет")
            return
        await state.clear()
        await show_refine_menu(callback.message, callback.from_user.id, edit=True)
        await callback.answer("Сделка обновлена")

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
        await show_refine_menu(callback.message, callback.from_user.id, edit=True)
        await callback.answer("Район обновлён" if district else "Ищу по всему городу")

    @router.callback_query(F.data == REFINE_BACK)
    async def handle_refine_back_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await state.clear()
        await show_refine_menu(callback.message, callback.from_user.id, edit=True)
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
            async with typing(target):
                result = await service.rerun_active_search(
                    telegram_user_id=callback.from_user.id,
                    username=callback.from_user.username,
                )
        except ActiveCriteriaNotFoundError:
            await target.answer(
                "Активные критерии не найдены. Сначала выполни поиск через /search."
            )
            return
        except (LocationInputError, SearchExecutionError) as exc:
            await target.answer(exc.user_message)
            return
        await send_search_execution(target, state, result)

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
            "🚫 Отклонено — вернуть можно в /trash" if rejected else "Квартира не найдена"
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
        await callback.answer(
            "🗑 Удалено (вернуть — /trash)" if removed else "Уже удалено"
        )

    @router.callback_query(F.data.startswith(RESTORE_TRASH_PREFIX))
    async def handle_restore_trash_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer()
            return
        external_id = callback.data[len(RESTORE_TRASH_PREFIX):]
        outcome = await service.restore_apartment(
            telegram_user_id=callback.from_user.id,
            external_id=external_id,
        )
        if outcome is not None and isinstance(callback.message, Message):
            with contextlib.suppress(Exception):
                await callback.message.delete()
        if outcome == "restored_to_saved":
            text = "♻️ Восстановлено — снова в /list"
        elif outcome == "unrejected":
            text = "♻️ Отклонение снято — снова появится в поиске"
        else:
            text = "Уже восстановлено"
        await callback.answer(text)

    @router.callback_query(F.data.startswith(PURGE_TRASH_PREFIX))
    async def handle_purge_trash_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer()
            return
        external_id = callback.data[len(PURGE_TRASH_PREFIX):]
        purged = await service.purge_trashed_apartment(
            telegram_user_id=callback.from_user.id,
            external_id=external_id,
        )
        if purged and isinstance(callback.message, Message):
            with contextlib.suppress(Exception):
                await callback.message.delete()
        await callback.answer(
            "🗑 Удалено навсегда — больше не покажу" if purged else "Уже удалено"
        )

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
            await message.answer(
                "Активные критерии не найдены. Сначала выполни поиск через /search."
            )
            return
        await state.clear()
        await show_refine_menu(message, message.from_user.id, edit=False)

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
            async with typing(message):
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

        async with typing(message):
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

        async with typing(message):
            result = await create_dialog_agent().handle_message(
                telegram_user_id=message.from_user.id,
                username=message.from_user.username,
                message=text,
                on_search_start=notify_search_start,
            )
        await send_dialog_turn(message, state, result)

    return router
