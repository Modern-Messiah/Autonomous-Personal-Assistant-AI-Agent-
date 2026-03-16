"""Database package."""

from db.checkpoints import build_checkpoint_config, get_async_postgres_checkpointer
from db.repositories import (
    MonitorTarget,
    get_active_search_criteria_record,
    get_monitor_settings_record,
    get_unseen_apartment_records,
    list_due_monitor_targets,
    list_seen_apartments,
    mark_apartments_seen,
    replace_active_search_criteria,
    touch_monitor_last_checked_at,
    upsert_apartment_records,
    upsert_monitor_settings,
    upsert_telegram_user,
)

__all__ = [
    "MonitorTarget",
    "build_checkpoint_config",
    "get_active_search_criteria_record",
    "get_async_postgres_checkpointer",
    "get_monitor_settings_record",
    "get_unseen_apartment_records",
    "list_due_monitor_targets",
    "list_seen_apartments",
    "mark_apartments_seen",
    "replace_active_search_criteria",
    "touch_monitor_last_checked_at",
    "upsert_apartment_records",
    "upsert_monitor_settings",
    "upsert_telegram_user",
]
