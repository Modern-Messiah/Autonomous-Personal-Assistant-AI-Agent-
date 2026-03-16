"""Telegram notifier used by the background monitor scheduler."""

from __future__ import annotations

from aiogram import Bot

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from bot.formatters import format_criteria, format_saved_apartments


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
        """Send monitoring update messages for newly discovered apartments."""
        await self._bot.send_message(
            telegram_user_id,
            "Найдены новые квартиры по сохраненным критериям.",
        )
        await self._bot.send_message(
            telegram_user_id,
            format_criteria(criteria),
        )
        await self._bot.send_message(
            telegram_user_id,
            format_saved_apartments(apartments, limit=len(apartments)),
        )
