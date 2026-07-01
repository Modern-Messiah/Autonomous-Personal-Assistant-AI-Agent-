"""Inline keyboards for Telegram dialog actions."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

REFINE_CALLBACK_DATA = "dialog:refine"
LIST_CALLBACK_DATA = "dialog:list"
SEARCH_MORE_CALLBACK_DATA = "dialog:more"
DELETE_SAVED_PREFIX = "saved:del:"
RESTORE_TRASH_PREFIX = "trash:restore:"
PURGE_TRASH_PREFIX = "trash:purge:"
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


def build_saved_item_keyboard(
    external_id: str, url: str | None = None
) -> InlineKeyboardMarkup:
    """Keyboard for a saved apartment: open it on Krisha (if known) + delete."""
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([InlineKeyboardButton(text="🌐 Открыть на Krisha", url=url)])
    rows.append(
        [
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=f"{DELETE_SAVED_PREFIX}{external_id}",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_trashed_item_keyboard(
    external_id: str, url: str | None = None
) -> InlineKeyboardMarkup:
    """Keyboard for a trashed apartment: open on Krisha (if known), restore, or
    delete it forever (permanent dismiss — stays hidden, not recoverable)."""
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([InlineKeyboardButton(text="🌐 Открыть на Krisha", url=url)])
    rows.append(
        [
            InlineKeyboardButton(
                text="♻️ Восстановить",
                callback_data=f"{RESTORE_TRASH_PREFIX}{external_id}",
            ),
            InlineKeyboardButton(
                text="🗑 Удалить навсегда",
                callback_data=f"{PURGE_TRASH_PREFIX}{external_id}",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_search_followup_keyboard() -> InlineKeyboardMarkup:
    """Navigation actions shown after the result cards (save/reject are per-card).

    The primary action re-runs the same search for the next batch of listings
    (already-seen ones are deduped out); refine/saved sit on the second row.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 Ещё варианты",
                    callback_data=SEARCH_MORE_CALLBACK_DATA,
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
            ],
        ]
    )
