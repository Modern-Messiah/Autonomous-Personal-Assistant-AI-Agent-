"""Formatting helpers for Telegram bot replies."""

from __future__ import annotations

from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment


def format_start_message() -> str:
    """Return onboarding message for `/start`."""
    return (
        "Krisha Agent готов к работе.\n\n"
        "Используй /search <запрос>, например:\n"
        "/search 2-комнатная квартира в Алматы до 45 млн\n\n"
        "Текущие критерии можно посмотреть через /criteria.\n"
        "Последние сохраненные варианты доступны через /list."
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
        parts.append(
            "Бюджет: "
            f"{budget_min} - {budget_max} KZT"
        )
    if criteria.rooms:
        parts.append(f"Комнаты: {', '.join(str(room) for room in criteria.rooms)}")
    if criteria.districts:
        parts.append(f"Районы: {', '.join(criteria.districts)}")
    if criteria.min_area_m2 is not None or criteria.max_area_m2 is not None:
        parts.append(
            "Площадь: "
            f"{criteria.min_area_m2 or 0:g} - {criteria.max_area_m2 or 0:g} м2"
        )
    parts.append(f"Страниц поиска: {criteria.page_limit}")
    return "Текущие критерии:\n" + "\n".join(parts)


def format_search_results(apartments: list[EnrichedApartment], *, limit: int = 3) -> str:
    """Render compact list of top apartments."""
    if not apartments:
        return "Подходящих квартир не найдено."

    lines = ["Нашел варианты:"]
    lines.extend(_format_apartment_lines(apartments, limit=limit))
    return "\n".join(lines)


def format_saved_apartments(apartments: list[EnrichedApartment], *, limit: int = 10) -> str:
    """Render saved apartments list for `/list` command."""
    if not apartments:
        return "Сохраненных квартир пока нет."

    lines = ["Сохраненные квартиры:"]
    lines.extend(_format_apartment_lines(apartments, limit=limit))
    return "\n".join(lines)


def _format_apartment_lines(
    apartments: list[EnrichedApartment],
    *,
    limit: int,
) -> list[str]:
    """Format apartment rows shared by search and saved list replies."""
    lines: list[str] = []
    for index, item in enumerate(apartments[:limit], start=1):
        apartment = item.apartment
        lines.append(
            (
                f"{index}. {apartment.title} | {apartment.price_kzt:,} KZT | "
                f"{apartment.area_m2 or '?'} м2 | {apartment.url}"
            ).replace(",", " ")
        )
        if item.score is not None:
            lines.append(
                f"   Score: {item.score.score:.1f} ({item.score.recommendation})"
            )
        if item.nearby_metro is not None or item.nearby_schools is not None:
            lines.append(
                "   Nearby: "
                f"schools={item.nearby_schools or 0}, "
                f"parks={item.nearby_parks or 0}, "
                f"metro={item.nearby_metro or 0}"
            )
    return lines
