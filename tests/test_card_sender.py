"""Tests for shared Telegram apartment-card delivery."""

from __future__ import annotations

import pytest

from agent.models.apartment import Apartment
from agent.models.enriched import EnrichedApartment
from bot.card_sender import send_apartment_card
from bot.keyboards import build_apartment_actions_keyboard


def item(*, photos: list[str]) -> EnrichedApartment:
    return EnrichedApartment(
        apartment=Apartment(
            external_id="card-1",
            source="krisha",
            url="https://krisha.kz/a/show/card-1",
            title="Card",
            price_kzt=20_000_000,
            city="Almaty",
            rooms=2,
            photos=photos,
        )
    )


@pytest.mark.asyncio
async def test_card_sender_falls_back_to_text_when_photo_fails() -> None:
    texts: list[str] = []

    async def send_text(text: str, **kwargs: object) -> None:
        del kwargs
        texts.append(text)

    async def send_photo(**kwargs: object) -> None:
        del kwargs
        raise RuntimeError("telegram rejected URL")

    await send_apartment_card(
        item(photos=["https://photos.example/card.jpg"]),
        index=1,
        reply_markup=build_apartment_actions_keyboard("card-1"),
        send_text=send_text,
        send_photo=send_photo,
        caption_suffix="⭐ подходит",
    )

    assert len(texts) == 1
    assert "⭐ подходит" in texts[0]
