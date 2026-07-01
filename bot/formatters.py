"""Formatting helpers for Telegram bot replies."""

from __future__ import annotations

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from bot.monitoring import format_monitor_interval
from bot.service import MonitorStatus

DEFAULT_SEARCH_RESULTS_LIMIT = 3

RECOMMENDATION_LABELS = {
    "strong_buy": "🟢 Брать",
    "consider": "🟡 Стоит посмотреть",
    "skip": "⚪ Можно пропустить",
}


def format_start_message() -> str:
    """Return onboarding message for `/start`."""
    return (
        "🏠 Krisha Agent — умный поиск квартир по Казахстану.\n"
        "Просто опишите, что ищете — обычным текстом или командой.\n\n"
        "Пример:\n"
        "/search 2-комнатная в Алматы до 45 млн, Бостандык\n\n"
        "🔍 Поиск\n"
        "• /search <запрос> — найти квартиры\n"
        "• /refine — уточнить критерии, /cancel — выйти из уточнения\n"
        "• /criteria — показать активные критерии\n\n"
        "💾 Избранное\n"
        "• /list — сохранённые квартиры\n"
        "• /trash — вернуть случайно удалённые\n"
        "• /foryou — персональная подборка (учится на ваших 💾 и 🚫)\n\n"
        "🔔 Мониторинг новых объявлений\n"
        "• /monitor — статус, /monitor on|off, /monitor interval 6h\n\n"
        "Работаю по всему Казахстану (Алматы, Астана, Шымкент, Караганда, Актобе…).\n"
        "Под каждой квартирой — кнопки 💾 Сохранить и 🚫 Отклонить, "
        "так подборка /foryou становится точнее."
    )


def format_criteria(criteria: SearchCriteria) -> str:
    """Render persisted search criteria for bot reply."""
    parts = [
        f"Город: {criteria.city}",
        f"Сделка: {'аренда' if criteria.deal_type == 'rent' else 'покупка'}",
        f"Тип: {criteria.property_type}",
    ]
    if criteria.min_price_kzt is not None or criteria.max_price_kzt is not None:
        budget_min = f"{criteria.min_price_kzt or 0:,}".replace(",", " ")
        budget_max = f"{criteria.max_price_kzt or 0:,}".replace(",", " ")
        parts.append(f"Бюджет: {budget_min} - {budget_max} KZT")
    if criteria.rooms:
        parts.append(f"Комнаты: {', '.join(str(room) for room in criteria.rooms)}")
    if criteria.districts:
        parts.append(f"Районы: {', '.join(criteria.districts)}")
    if criteria.min_area_m2 is not None or criteria.max_area_m2 is not None:
        parts.append(f"Площадь: {criteria.min_area_m2 or 0:g} - {criteria.max_area_m2 or 0:g} м2")
    parts.append(f"Страниц поиска: {criteria.page_limit}")
    return "Текущие критерии:\n" + "\n".join(parts)


def clean_listing_url(url: str) -> str:
    """Drop tracking query/fragment so the link is short and clean."""
    return url.split("?", 1)[0].split("#", 1)[0]


def format_apartment_card(item: EnrichedApartment, *, index: int | None = None) -> str:
    """Render one apartment as a rich plain-text card (photo caption / list row)."""
    apartment = item.apartment
    prefix = f"{index}. " if index is not None else ""
    price = f"{apartment.price_kzt:,}".replace(",", " ")

    lines = [f"🏠 {prefix}{_format_specs(apartment)}"]
    if apartment.area_m2 and apartment.area_m2 > 0:
        per_m2 = f"{round(apartment.price_kzt / apartment.area_m2):,}".replace(",", " ")
        lines.append(f"💰 {price} ₸  (≈ {per_m2} ₸/м²)")
    else:
        lines.append(f"💰 {price} ₸")
    lines.append(f"📍 {_format_location(apartment)}")
    if apartment.published_at is not None:
        lines.append(f"📅 Опубликовано: {apartment.published_at:%d.%m.%Y}")
    if item.mortgage_monthly_payment_kzt:
        payment = f"{item.mortgage_monthly_payment_kzt:,}".replace(",", " ")
        lines.append(f"🏦 Ипотека: ~{payment} ₸/мес")
    if (
        item.nearby_schools is not None
        or item.nearby_parks is not None
        or item.nearby_metro is not None
    ):
        lines.append(
            f"🏫 школы: {item.nearby_schools or 0} · "
            f"🌳 парки: {item.nearby_parks or 0} · "
            f"🚇 метро: {item.nearby_metro or 0}"
        )
    if item.score is not None:
        label = RECOMMENDATION_LABELS.get(
            item.score.recommendation, item.score.recommendation
        )
        lines.append(f"{label} · {item.score.score:.0f}/100")
        lines.extend(f"   • {reason}" for reason in item.score.reasons[:3])
    lines.append(f"🔗 {clean_listing_url(apartment.url)}")
    return "\n".join(lines)


def format_search_results(
    apartments: list[EnrichedApartment],
    *,
    limit: int = DEFAULT_SEARCH_RESULTS_LIMIT,
) -> str:
    """Render top apartments as text cards (fallback when photos can't be sent)."""
    if not apartments:
        return "Подходящих квартир не найдено."
    cards = [
        format_apartment_card(item, index=index)
        for index, item in enumerate(apartments[:limit], start=1)
    ]
    return "Нашёл варианты:\n\n" + "\n\n".join(cards)


def format_saved_apartments(apartments: list[EnrichedApartment], *, limit: int = 10) -> str:
    """Render saved apartments list for `/list` command."""
    if not apartments:
        return "Сохраненных квартир пока нет."
    cards = [
        format_apartment_card(item, index=index)
        for index, item in enumerate(apartments[:limit], start=1)
    ]
    return "Сохраненные квартиры:\n\n" + "\n\n".join(cards)


def format_monitor_status(status: MonitorStatus | None) -> str:
    """Render persisted monitor settings for `/monitor` command."""
    if status is None:
        return (
            "Мониторинг пока не настроен.\n"
            "Используй /monitor on, /monitor off или /monitor interval 6h."
        )

    state = "включен" if status.enabled else "выключен"
    interval = format_monitor_interval(status.interval_minutes)
    return (
        "Статус мониторинга:\n"
        f"Состояние: {state}\n"
        f"Интервал: {interval}"
    )


def _format_specs(apartment: Apartment) -> str:
    parts = [f"{apartment.rooms}-комнатная" if apartment.rooms else "Квартира"]
    if apartment.area_m2 is not None:
        parts.append(f"{apartment.area_m2:g} м²")
    if apartment.floor:
        parts.append(f"этаж {apartment.floor}")
    return " · ".join(parts)


def _format_location(apartment: Apartment) -> str:
    # The parsed address already carries city/district/street, so show it as-is
    # and only fall back to city (+ district) when there is no address.
    if apartment.address:
        return apartment.address
    if apartment.district:
        return f"{apartment.city}, {apartment.district}"
    return apartment.city
