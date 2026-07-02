"""Inline keyboards for Telegram dialog actions."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from agent.locations import LOCATIONS

REFINE_CALLBACK_DATA = "dialog:refine"
LIST_CALLBACK_DATA = "dialog:list"
SEARCH_MORE_CALLBACK_DATA = "dialog:more"
DELETE_SAVED_PREFIX = "saved:del:"
RESTORE_TRASH_PREFIX = "trash:restore:"
PURGE_TRASH_PREFIX = "trash:purge:"
APT_SAVE_PREFIX = "apt:save:"
APT_REJECT_PREFIX = "apt:reject:"

# Guided-refine menu callbacks.
REFINE_FIELD_PREFIX = "refine:field:"       # + city|deal|district|rooms|budget|area
REFINE_SET_CITY_PREFIX = "refine:city:"     # + canonical city
REFINE_SET_DEAL_PREFIX = "refine:deal:"     # + sale|rent
REFINE_SET_DISTRICT_PREFIX = "refine:distr:"  # + canonical district, or "*" to clear
REFINE_CITY_OTHER = "refine:city_other"
REFINE_BACK = "refine:back"
REFINE_RUN = "refine:run"
REFINE_DISTRICT_CLEAR = "*"

# Cities offered as quick buttons in the guided refine (canonical names); any
# other city is entered as free text via "Другой город".
_REFINE_QUICK_CITIES = (
    "Almaty",
    "Astana",
    "Shymkent",
    "Karaganda",
    "Aktobe",
    "Atyrau",
    "Pavlodar",
    "Taraz",
)


def _rows(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def build_refine_menu_keyboard(city: str | None) -> InlineKeyboardMarkup:
    """Guided-refine menu: pick a field to change, then search.

    The district row is shown only when the current city actually has districts.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🏙 Город", callback_data=f"{REFINE_FIELD_PREFIX}city"),
            InlineKeyboardButton(text="🤝 Сделка", callback_data=f"{REFINE_FIELD_PREFIX}deal"),
        ],
    ]
    if city is not None and LOCATIONS.districts_for_city(city):
        rows.append(
            [InlineKeyboardButton(text="📍 Район", callback_data=f"{REFINE_FIELD_PREFIX}district")]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🚪 Комнаты", callback_data=f"{REFINE_FIELD_PREFIX}rooms"),
            InlineKeyboardButton(text="💰 Бюджет", callback_data=f"{REFINE_FIELD_PREFIX}budget"),
            InlineKeyboardButton(text="📐 Площадь", callback_data=f"{REFINE_FIELD_PREFIX}area"),
        ]
    )
    rows.append([InlineKeyboardButton(text="✅ Искать", callback_data=REFINE_RUN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_refine_city_keyboard() -> InlineKeyboardMarkup:
    """City picker: popular cities as buttons + free-text option."""
    buttons: list[InlineKeyboardButton] = []
    for canonical in _REFINE_QUICK_CITIES:
        city = LOCATIONS.get_city(canonical)
        if city is None:
            continue
        buttons.append(
            InlineKeyboardButton(
                text=city.name_ru,
                callback_data=f"{REFINE_SET_CITY_PREFIX}{canonical}",
            )
        )
    rows = _rows(buttons, 2)
    rows.append(
        [InlineKeyboardButton(text="✏️ Другой город", callback_data=REFINE_CITY_OTHER)]
    )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=REFINE_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_refine_deal_keyboard() -> InlineKeyboardMarkup:
    """Deal-type picker."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Купить", callback_data=f"{REFINE_SET_DEAL_PREFIX}sale"
                ),
                InlineKeyboardButton(
                    text="🔑 Снять", callback_data=f"{REFINE_SET_DEAL_PREFIX}rent"
                ),
            ],
            [InlineKeyboardButton(text="← Назад", callback_data=REFINE_BACK)],
        ]
    )


def build_refine_district_keyboard(city: str) -> InlineKeyboardMarkup:
    """District picker for a city that has districts, plus a 'whole city' reset."""
    buttons = [
        InlineKeyboardButton(
            text=district.name_ru,
            callback_data=f"{REFINE_SET_DISTRICT_PREFIX}{district.canonical}",
        )
        for district in LOCATIONS.districts_for_city(city)
    ]
    rows = _rows(buttons, 2)
    rows.append(
        [
            InlineKeyboardButton(
                text="🏙 Весь город",
                callback_data=f"{REFINE_SET_DISTRICT_PREFIX}{REFINE_DISTRICT_CLEAR}",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=REFINE_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_refine_back_keyboard() -> InlineKeyboardMarkup:
    """Single 'back to menu' button, shown under a typed-value prompt."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Назад", callback_data=REFINE_BACK)]]
    )


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
