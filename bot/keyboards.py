"""Inline keyboards for Telegram dialog actions."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

REFINE_CALLBACK_DATA = "dialog:refine"
LIST_CALLBACK_DATA = "dialog:list"


def build_search_followup_keyboard() -> InlineKeyboardMarkup:
    """Return inline actions shown after search results."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Уточнить критерии",
                    callback_data=REFINE_CALLBACK_DATA,
                ),
                InlineKeyboardButton(
                    text="Сохраненные",
                    callback_data=LIST_CALLBACK_DATA,
                ),
            ]
        ]
    )
