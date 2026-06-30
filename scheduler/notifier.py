"""Telegram notifier used by the background monitor scheduler."""

from __future__ import annotations

from functools import partial

from aiogram import Bot

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from bot.card_sender import send_apartment_card
from bot.formatters import format_criteria
from bot.keyboards import build_apartment_actions_keyboard


class TelegramMonitorNotifier:
    """Sends monitor updates to Telegram users."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def __call__(
        self,
        telegram_user_id: int,
        criteria: SearchCriteria,
        apartments: list[EnrichedApartment],
    ) -> None:
        """Send a monitor update as one photo card per newly discovered apartment."""
        await self._bot.send_message(
            telegram_user_id,
            "🔔 Найдены новые квартиры по сохранённым критериям.",
        )
        await self._bot.send_message(telegram_user_id, format_criteria(criteria))
        for index, item in enumerate(apartments, start=1):
            keyboard = build_apartment_actions_keyboard(item.apartment.external_id)
            await send_apartment_card(
                item,
                index=index,
                reply_markup=keyboard,
                send_text=partial(self._bot.send_message, telegram_user_id),
                send_photo=partial(self._bot.send_photo, telegram_user_id),
            )
