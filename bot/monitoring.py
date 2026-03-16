"""Helpers for Telegram monitor command parsing and formatting."""

from __future__ import annotations

import re

DEFAULT_MONITOR_INTERVAL_MINUTES = 360
MIN_MONITOR_INTERVAL_MINUTES = 15
MAX_MONITOR_INTERVAL_MINUTES = 7 * 24 * 60

_INTERVAL_PATTERN = re.compile(r"^\s*(\d+)\s*([mhd])\s*$", re.IGNORECASE)
_UNIT_TO_MINUTES = {
    "m": 1,
    "h": 60,
    "d": 24 * 60,
}


def parse_monitor_interval(value: str) -> int:
    """Parse compact duration like `30m`, `6h`, or `1d` into minutes."""
    match = _INTERVAL_PATTERN.fullmatch(value)
    if match is None:
        msg = "interval must look like 30m, 6h, or 1d"
        raise ValueError(msg)

    amount = int(match.group(1))
    unit = match.group(2).lower()
    minutes = amount * _UNIT_TO_MINUTES[unit]
    if minutes < MIN_MONITOR_INTERVAL_MINUTES:
        msg = "interval must be at least 15 minutes"
        raise ValueError(msg)
    if minutes > MAX_MONITOR_INTERVAL_MINUTES:
        msg = "interval must be at most 7 days"
        raise ValueError(msg)
    return minutes


def format_monitor_interval(minutes: int) -> str:
    """Format interval minutes into compact human-readable form."""
    if minutes % (24 * 60) == 0:
        return f"{minutes // (24 * 60)}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"
