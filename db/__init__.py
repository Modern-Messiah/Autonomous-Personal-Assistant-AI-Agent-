"""Database package."""

from db.checkpoints import build_checkpoint_config, get_async_postgres_checkpointer
from db.repositories import (
    get_active_search_criteria_record,
    get_monitor_settings_record,
    list_seen_apartments,
    mark_apartments_seen,
    replace_active_search_criteria,
    upsert_apartment_records,
    upsert_monitor_settings,
    upsert_telegram_user,
)

__all__ = [
    "build_checkpoint_config",
    "get_active_search_criteria_record",
    "get_async_postgres_checkpointer",
    "get_monitor_settings_record",
    "list_seen_apartments",
    "mark_apartments_seen",
    "replace_active_search_criteria",
    "upsert_apartment_records",
    "upsert_monitor_settings",
    "upsert_telegram_user",
]
