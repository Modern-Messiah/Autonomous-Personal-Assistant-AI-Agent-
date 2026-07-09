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


@pytest.mark.asyncio
async def test_card_sender_caption_respects_telegram_limit() -> None:
    from bot.formatters import TELEGRAM_PHOTO_CAPTION_LIMIT, telegram_text_length

    captions: list[str] = []

    async def send_text(text: str, **kwargs: object) -> None:
        del kwargs
        captions.append(text)

    async def send_photo(**kwargs: object) -> None:
        captions.append(str(kwargs["caption"]))

    huge_item = item(photos=["https://photos.example/card.jpg"])
    huge_item = huge_item.model_copy(
        update={
            "apartment": huge_item.apartment.model_copy(
                update={"description": "Дом мечты у парка, торг уместен. 🏡 " * 60}  # noqa: RUF001
            )
        }
    )

    await send_apartment_card(
        huge_item,
        index=1,
        reply_markup=build_apartment_actions_keyboard("card-1"),
        send_text=send_text,
        send_photo=send_photo,
        caption_suffix="⭐ подходит",
    )

    assert len(captions) == 1
    # the suffix survived AND the whole caption stays sendable as a photo
    assert captions[0].endswith("⭐ подходит")
    assert telegram_text_length(captions[0]) <= TELEGRAM_PHOTO_CAPTION_LIMIT
    # the description used the freed space (not the old 160-char teaser)
    assert captions[0].count("Дом мечты") > 5
