"""Telegram notifier used by the background monitor scheduler."""

from __future__ import annotations

from aiogram import Bot

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from bot.formatters import format_apartment_card, format_criteria
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
            caption = format_apartment_card(item, index=index)
            keyboard = build_apartment_actions_keyboard(item.apartment.external_id)
            photo = item.apartment.photos[0] if item.apartment.photos else None
            if photo is not None:
                try:
                    await self._bot.send_photo(
                        telegram_user_id,
                        photo=photo,
                        caption=caption,
                        reply_markup=keyboard,
                    )
                    continue
                except Exception:
                    pass
            await self._bot.send_message(telegram_user_id, caption, reply_markup=keyboard)
