"""Parser canary: periodically verify krisha parsing works and alert on a break.

The whole bot depends on scraping krisha, and a markup change or a block makes
searches silently return nothing. The canary parses a reference search page on a
schedule, checks that the key fields still extract, and pings the admin chat when
they stop — so a breakage is noticed before users hit empty results.
"""

from __future__ import annotations

import logging

from aiogram import Bot

from agent.models.criteria import SearchCriteria
from agent.nodes.search_node import build_playwright_context_factory
from agent.tools import KrishaParser, build_redis_client
from agent.tools.krisha_parser import AntiBotBlockedError, ParserHealthReport
from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Placeholder user id for canary criteria (SearchCriteria requires a positive id).
# check_health never claims dedup keys, so this never touches a real user's data.
CANARY_USER_ID = 1


def build_canary_criteria(settings: Settings) -> SearchCriteria:
    """A broad, always-populated search so an empty result means a real break."""
    return SearchCriteria(
        user_id=CANARY_USER_ID,
        city=settings.scheduler.canary_city,
        deal_type="sale",
        property_type="apartment",
        rooms=[2],
        page_limit=1,
    )


def _failed_report(reason: str) -> ParserHealthReport:
    return ParserHealthReport(
        ok=False,
        listing_count=0,
        previews_with_price=0,
        previews_with_specs=0,
        detail_checked=False,
        failures=[reason],
    )


async def collect_report(settings: Settings) -> ParserHealthReport:
    """Run the parser canary against krisha and return a health report."""
    parser = KrishaParser(
        redis_client=build_redis_client(settings.redis.redis_url),
        min_delay_seconds=settings.parser.min_delay_seconds,
        max_delay_seconds=settings.parser.max_delay_seconds,
        timeout_ms=settings.parser.timeout_ms,
        dedup_ttl_seconds=settings.parser.dedup_ttl_seconds,
        max_results=settings.parser.max_results,
    )
    factory = build_playwright_context_factory(parser)
    criteria = build_canary_criteria(settings)
    try:
        async with factory() as context:
            return await parser.check_health(context, criteria=criteria)
    except AntiBotBlockedError as exc:
        return _failed_report(f"blocked by anti-bot ({exc})")
    except Exception as exc:
        # The canary must never crash the worker; any unexpected error is itself
        # a signal the parsing path is broken.
        return _failed_report(f"canary crashed: {exc!r}")


def format_canary_alert(report: ParserHealthReport) -> str:
    """Render a Telegram alert describing why the parser canary failed."""
    lines = [
        "🚨 Канарейка: парсер krisha похоже сломался.",
        "",
        f"Объявлений распознано: {report.listing_count}",
        f"Распознано цен: {report.previews_with_price}",
        f"Распознано комнат/площадей: {report.previews_with_specs}",
        f"Детальных страниц проверено: {report.details_checked}",
        (
            f"Автор/описание/дата/состояние: {report.details_with_posted_by}/"
            f"{report.details_with_description}/{report.details_with_published_at}/"
            f"{report.details_with_condition} из {report.details_checked}"
        ),
        "",
        "Что не так:",
    ]
    lines.extend(f"• {item}" for item in report.failures)
    return "\n".join(lines)


async def deliver_canary_alert(
    bot: Bot | None,
    chat_id: int | None,
    report: ParserHealthReport,
) -> None:
    """Log the canary outcome and alert the admin chat when the parser is broken."""
    if report.ok:
        logger.info("parser canary ok: %s listings parsed", report.listing_count)
        return

    logger.warning("parser canary FAILED: %s", report.failures)
    if bot is None or chat_id is None:
        return
    try:
        await bot.send_message(chat_id, format_canary_alert(report))
    except Exception:
        logger.exception("failed to deliver parser canary alert")


async def run_parser_canary(
    *,
    bot: Bot | None,
    settings: Settings | None = None,
) -> ParserHealthReport:
    """Collect a parser health report and alert the admin chat on failure."""
    active = settings or get_settings()
    report = await collect_report(active)
    await deliver_canary_alert(bot, active.scheduler.canary_admin_chat_id, report)
    return report
