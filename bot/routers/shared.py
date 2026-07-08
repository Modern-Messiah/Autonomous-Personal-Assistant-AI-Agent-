"""Helpers shared by the feature routers (menu, cards, dialog rendering)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from agent.models.enriched import EnrichedApartment
from bot.card_sender import send_apartment_card
from bot.dialog_agent import DialogTurnResult
from bot.formatters import (
    DEFAULT_SEARCH_RESULTS_LIMIT,
    BatchPriceStats,
    clean_listing_url,
    format_criteria,
    format_search_results,
)
from bot.keyboards import (
    build_apartment_actions_keyboard,
    build_refine_menu_keyboard,
    build_saved_item_keyboard,
    build_search_followup_keyboard,
    build_trashed_item_keyboard,
)
from bot.service import RecommendationResult, SearchBotService, SearchExecution
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
NO_ACTIVE_CRITERIA_MESSAGE = "Активные критерии не найдены. Сначала выполни поиск через /search."


def batch_price_stats(apartments: list[EnrichedApartment]) -> BatchPriceStats | None:
    """₸/м² average + size of the batch, for the per-card price comparison.

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
    return BatchPriceStats(avg_price_per_m2=sum(values) / len(values), count=len(values))


@contextlib.asynccontextmanager
async def typing_action(message: Message) -> AsyncIterator[None]:
    """Keep a live 'печатает…' indicator alive while a slow handler runs."""
    if message.bot is None:
        yield
        return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        yield


class RouterHelpers:
    """Rendering flows that several feature routers share."""

    def __init__(self, service: SearchBotService) -> None:
        self._service = service

    async def show_refine_menu(
        self,
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
        criteria = await self._service.get_active_criteria(telegram_user_id=telegram_user_id)
        if criteria is None:
            await target.answer(NO_ACTIVE_CRITERIA_MESSAGE)
            return False
        text = f"{REFINE_MENU_HINT}\n\n{format_criteria(criteria)}"
        markup = build_refine_menu_keyboard(
            criteria.city,
            owner_only=criteria.owner_only,
            is_rent=criteria.deal_type == "rent",
        )
        if edit:
            with contextlib.suppress(Exception):
                await target.edit_text(text, reply_markup=markup)
                return True
        await target.answer(text, reply_markup=markup)
        return True

    async def send_search_execution(
        self,
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

        price_stats = batch_price_stats(presented_apartments)
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
                price_stats=price_stats,
            )

        await message.answer(
            "Что делаем дальше?",
            reply_markup=build_search_followup_keyboard(),
        )
        await state.set_state(SearchDialogStates.waiting_for_feedback)

    async def send_saved_list(self, target: Message, telegram_user_id: int) -> None:
        apartments = await self._service.get_saved_apartments(telegram_user_id=telegram_user_id)
        if not apartments:
            await target.answer(
                "💾 Сохранённых квартир пока нет.\n"
                "Запустите поиск через /search и нажмите «💾 Сохранить» на карточке."
            )
            return
        total = await self._service.count_saved_apartments(telegram_user_id=telegram_user_id)
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

    async def send_trash_list(self, target: Message, telegram_user_id: int) -> None:
        apartments = await self._service.get_trashed_apartments(telegram_user_id=telegram_user_id)
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

    async def send_recommendations(self, target: Message, result: RecommendationResult) -> None:
        if not result.recommendations:
            await target.answer(
                "Сейчас нет свежих вариантов под ваш вкус. "
                "Загляните позже или уточните поиск через /search."
            )
            return
        await target.answer(
            f"⭐ Подобрал под ваши предпочтения ({len(result.recommendations)}):"
        )
        price_stats = batch_price_stats(
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
                price_stats=price_stats,
            )

    async def send_dialog_turn(
        self,
        message: Message,
        state: FSMContext,
        result: DialogTurnResult,
    ) -> None:
        for item in result.messages:
            await message.answer(item)

        if result.show_saved and message.from_user is not None:
            await self.send_saved_list(message, message.from_user.id)

        if result.search_execution is not None:
            await self.send_search_execution(message, state, result.search_execution)
            return

        if result.next_state == "clear":
            await state.clear()
            return

        await state.set_state(SearchDialogStates.waiting_for_feedback)
