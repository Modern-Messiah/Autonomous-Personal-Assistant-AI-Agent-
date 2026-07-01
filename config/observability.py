"""Shared logging and optional telemetry initialization."""

from __future__ import annotations

import logging
import os

import sentry_sdk

from config.settings import Settings, get_settings


def configure_observability(settings: Settings | None = None) -> None:
    """Configure process logging and optional telemetry without exposing secrets."""
    active = settings or get_settings()
    logging.basicConfig(
        level=active.app.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )

    # A malformed/placeholder DSN must never crash the whole process — observability
    # setup should degrade to "disabled", not take the bot down in a restart loop.
    sentry_enabled = False
    if active.api.sentry_dsn is not None:
        try:
            sentry_sdk.init(dsn=active.api.sentry_dsn, environment=active.app.env)
            sentry_enabled = True
        except Exception:
            logging.getLogger(__name__).warning(
                "invalid API__SENTRY_DSN; error tracking disabled", exc_info=True
            )

    langsmith_key = active.api.langsmith_api_key
    langsmith_project = active.api.langsmith_project
    langsmith_enabled = langsmith_key is not None and langsmith_project is not None
    for name in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"):
        os.environ.pop(name, None)
    if langsmith_key is not None and langsmith_project is not None:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_API_KEY"] = langsmith_key.get_secret_value()
        os.environ["LANGSMITH_PROJECT"] = langsmith_project

    logging.getLogger(__name__).info(
        "observability configured sentry=%s langsmith=%s",
        sentry_enabled,
        langsmith_enabled,
    )
