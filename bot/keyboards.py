"""Inline keyboards for Telegram dialog actions."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

REFINE_CALLBACK_DATA = "dialog:refine"
LIST_CALLBACK_DATA = "dialog:list"
DELETE_SAVED_PREFIX = "saved:del:"
APT_SAVE_PREFIX = "apt:save:"
APT_REJECT_PREFIX = "apt:reject:"


def build_apartment_actions_keyboard(external_id: str) -> InlineKeyboardMarkup:
    """Per-apartment Save/Reject buttons shown under each search result card."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💾 Сохранить",
                    callback_data=f"{APT_SAVE_PREFIX}{external_id}",
                ),
                InlineKeyboardButton(
                    text="🚫 Отклонить",
                    callback_data=f"{APT_REJECT_PREFIX}{external_id}",
                ),
            ]
        ]
    )


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
    """Navigation actions shown after the result cards (save/reject are per-card)."""
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
