"""Shared Telegram apartment-card delivery with photo fallback."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from agent.models.enriched import EnrichedApartment
from bot.formatters import (
    TELEGRAM_PHOTO_CAPTION_LIMIT,
    BatchPriceStats,
    format_apartment_card,
    telegram_text_length,
)

logger = logging.getLogger(__name__)
Sender = Callable[..., Awaitable[Any]]


async def send_apartment_card(
    item: EnrichedApartment,
    *,
    index: int,
    reply_markup: InlineKeyboardMarkup,
    send_text: Sender,
    send_photo: Sender,
    caption_suffix: str | None = None,
    price_stats: BatchPriceStats | None = None,
) -> None:
    """Send one photo card, falling back to text when Telegram rejects the photo."""
    # Let the description fill everything left under Telegram's 1024-unit
    # caption cap (short ones fit whole) instead of a fixed 160-char teaser.
    budget = TELEGRAM_PHOTO_CAPTION_LIMIT
    if caption_suffix:
        budget -= telegram_text_length(f"\n\n{caption_suffix}")
    caption = format_apartment_card(
        item, index=index, price_stats=price_stats, caption_budget=budget
    )
    if caption_suffix:
        caption = f"{caption}\n\n{caption_suffix}"
    photo = item.apartment.photos[0] if item.apartment.photos else None
    if photo is not None:
        try:
            await send_photo(photo=photo, caption=caption, reply_markup=reply_markup)
            return
        except Exception:
            logger.warning(
                "telegram rejected apartment photo external_id=%s",
                item.apartment.external_id,
                exc_info=True,
            )
    await send_text(caption, reply_markup=reply_markup)
