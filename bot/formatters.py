"""Formatting helpers for Telegram bot replies."""

from __future__ import annotations

from dataclasses import dataclass

from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from bot.monitoring import format_monitor_interval
from bot.service import MonitorStatus

# Show everything the pipeline fetches and scores (PARSER__MAX_RESULTS caps the
# fetch at 6), instead of cutting the presentation to a shorter top.
DEFAULT_SEARCH_RESULTS_LIMIT = 6

RECOMMENDATION_LABELS = {
    "strong_buy": "🟢 Брать",
    "consider": "🟡 Стоит посмотреть",
    "skip": "⚪ Можно пропустить",
}


def _nearby_distance(distance_m: int | None) -> str:
    """Render the distance to the nearest object, e.g. ' (480 м)' or ' (1.2 км)'."""
    if distance_m is None:
        return ""
    if distance_m >= 1000:
        return f" ({distance_m / 1000:.1f} км)"
    return f" ({distance_m} м)"


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
    if criteria.deal_type == "rent":
        period_labels = {"daily": "посуточно", "hourly": "по часам"}
        period = period_labels.get(criteria.rent_period or "", "помесячно")
        deal_label = f"аренда ({period})"
    else:
        deal_label = "покупка"
    parts = [
        f"Город: {criteria.city}",
        f"Сделка: {deal_label}",
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
    if criteria.owner_only:
        parts.append("Только от хозяина: да")
    parts.append(f"Страниц поиска: {criteria.page_limit}")
    return "Текущие критерии:\n" + "\n".join(parts)


def clean_listing_url(url: str) -> str:
    """Drop tracking query/fragment and show the canonical krisha.kz host."""
    cleaned = url.split("?", 1)[0].split("#", 1)[0]
    return cleaned.replace("//m.krisha.kz", "//krisha.kz")


@dataclass(slots=True, frozen=True)
class BatchPriceStats:
    """₸/м² statistics of the currently presented batch of listings."""

    avg_price_per_m2: float
    count: int


def _market_or_batch_line(
    apartment: Apartment, price_per_m2: float, stats: BatchPriceStats | None
) -> str | None:
    """Price context: prefer krisha's city-market verdict, fall back to the batch.

    krisha gives a signed «на X% дешевле/дороже рынка города»; the city average
    ₸/м² is derived from this listing's own ₸/м² and that percent.
    """
    diff = apartment.market_diff_percent
    if diff is not None:
        factor = 1 + diff / 100
        avg = f"{round(price_per_m2 / factor):,}".replace(",", " ") if factor > 0 else None
        percent = abs(round(diff))
        if percent < 1:
            return "🏙 цена за м² на уровне рынка города"
        word = "дешевле" if diff < 0 else "дороже"
        tail = f" (сред. {avg} ₸/м²)" if avg else ""
        return f"🏙 на {percent}% {word} рынка города{tail}"
    if stats is not None and stats.avg_price_per_m2 > 0:
        return _price_vs_batch(price_per_m2, stats)
    return None


def _price_vs_batch(price_per_m2: float, stats: BatchPriceStats) -> str:
    """One-line comparison of this listing's ₸/м² against the batch average.

    Names what it compares against — the average over the N listings the user
    is looking at right now — so «среднее» is not mistaken for a market-wide one.
    """
    avg = f"{round(stats.avg_price_per_m2):,}".replace(",", " ")
    context = f"по {stats.count} вариантам (среднее {avg} ₸/м²)"
    diff = (price_per_m2 - stats.avg_price_per_m2) / stats.avg_price_per_m2
    percent = abs(round(diff * 100))
    if percent < 3:
        return f"📊 цена за м² на уровне среднего {context}"
    word = "дешевле" if diff < 0 else "дороже"
    return f"📊 на {percent}% {word} среднего за м² {context}"


def format_apartment_card(
    item: EnrichedApartment,
    *,
    index: int | None = None,
    price_stats: BatchPriceStats | None = None,
) -> str:
    """Render one apartment as a rich plain-text card (photo caption / list row).

    ``price_stats`` carries the ₸/м² average of the current selection; when
    provided, the card shows how this listing compares against it.
    """
    apartment = item.apartment
    prefix = f"{index}. " if index is not None else ""
    price = f"{apartment.price_kzt:,}".replace(",", " ")

    lines = [f"🏠 {prefix}{_format_specs(apartment)}"]
    if apartment.area_m2 and apartment.area_m2 > 0:
        price_per_m2 = apartment.price_kzt / apartment.area_m2
        per_m2 = f"{round(price_per_m2):,}".replace(",", " ")
        lines.append(f"💰 {price} ₸  (≈ {per_m2} ₸/м²)")
        context = _market_or_batch_line(apartment, price_per_m2, price_stats)
        if context is not None:
            lines.append(context)
    else:
        lines.append(f"💰 {price} ₸")
    lines.append(f"📍 {_format_location(apartment)}")
    if apartment.posted_by == "owner":
        lines.append("👤 От хозяина")
    elif apartment.posted_by == "agent":
        agency = f" ({apartment.agency_name})" if apartment.agency_name else ""
        lines.append(f"🏢 От риелтора{agency}")
    elif apartment.posted_by == "developer":
        lines.append("🏗 От застройщика")
    features = _format_features(apartment)
    if features:
        lines.append(features)
    published = apartment.published_at
    if published is not None:
        line = f"📅 Опубликовано {published:%d.%m.%Y}"
        days = apartment.days_on_market()
        if days == 0:
            line += " · 🆕 сегодня"
        elif days is not None:
            line += f" · висит {days} {_plural_days(days)}"
        lines.append(line)
    if item.mortgage_monthly_payment_kzt:
        payment = f"{item.mortgage_monthly_payment_kzt:,}".replace(",", " ")
        lines.append(f"🏦 Ипотека: ~{payment} ₸/мес")
    # Show only the categories we actually have data for. Metro is unknown/None in
    # cities without a metro, so its chip is dropped there instead of "метро: 0".
    nearby_chips: list[str] = []
    if item.nearby_schools is not None:
        nearby_chips.append(
            f"🏫 школы: {item.nearby_schools}{_nearby_distance(item.nearby_school_m)}"
        )
    if item.nearby_parks is not None:
        nearby_chips.append(
            f"🌳 парки: {item.nearby_parks}{_nearby_distance(item.nearby_park_m)}"
        )
    if item.nearby_metro is not None:
        # A true zero means "checked, no station within the search radius" —
        # say that in words instead of a cryptic "метро: 0".
        nearby_chips.append(
            "🚇 метро: нет рядом (2 км+)"
            if item.nearby_metro == 0
            else f"🚇 метро: {item.nearby_metro}{_nearby_distance(item.nearby_metro_m)}"
        )
    if nearby_chips:
        lines.append(" · ".join(nearby_chips))
    if item.score is not None:
        label = RECOMMENDATION_LABELS.get(
            item.score.recommendation, item.score.recommendation
        )
        lines.append(f"{label} · {item.score.score:.0f}/100")
        lines.extend(f"   • {reason}" for reason in item.score.reasons[:3])
    snippet = _description_snippet(apartment.description)
    if snippet:
        lines.append(f"📝 {snippet}")
    # No raw 🔗 line: the link is the "🌐 Открыть на Krisha" button on every card.
    return "\n".join(lines)


def _format_features(apartment: Apartment) -> str | None:
    """One compact features line: year, building type, ceiling, furniture."""
    parts: list[str] = []
    if apartment.build_year is not None:
        parts.append(str(apartment.build_year))
    if apartment.building_type:
        parts.append(apartment.building_type)
    if apartment.ceiling_height_m is not None:
        parts.append(f"потолки {apartment.ceiling_height_m:g} м")
    if apartment.condition:
        parts.append(f"🔨 {apartment.condition}")
    if apartment.furnished:
        parts.append(f"🛋 {apartment.furnished}")
    return "🏗 " + " · ".join(parts) if parts else None


def _plural_days(count: int) -> str:
    """Russian plural for «день» — 1 день, 2 дня, 5 дней, 21 день, 112 дней."""
    if 11 <= count % 100 <= 14:
        return "дней"
    last = count % 10
    if last == 1:
        return "день"
    if 2 <= last <= 4:
        return "дня"
    return "дней"


def _description_snippet(description: str | None, *, limit: int = 160) -> str | None:
    """One-line teaser of the description; the full text goes to the AI scorer."""
    if not description:
        return None
    flat = " ".join(description.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


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
