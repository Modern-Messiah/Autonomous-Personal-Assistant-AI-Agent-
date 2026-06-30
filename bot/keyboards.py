"""Inline keyboards for Telegram dialog actions."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

SAVE_CALLBACK_DATA = "dialog:save"
REJECT_CALLBACK_DATA = "dialog:reject"
REFINE_CALLBACK_DATA = "dialog:refine"
LIST_CALLBACK_DATA = "dialog:list"
DELETE_SAVED_PREFIX = "saved:del:"


def build_saved_item_keyboard(external_id: str) -> InlineKeyboardMarkup:
    """Return a one-button keyboard to delete a saved apartment."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"{DELETE_SAVED_PREFIX}{external_id}",
                ),
            ]
        ]
    )


def build_search_followup_keyboard() -> InlineKeyboardMarkup:
    """Return inline actions shown after search results."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сохранить",
                    callback_data=SAVE_CALLBACK_DATA,
                ),
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=REJECT_CALLBACK_DATA,
                ),
            ],
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
