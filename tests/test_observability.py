"""Tests for shared runtime observability configuration."""

from __future__ import annotations

import logging
import os

import sentry_sdk

from config.observability import configure_observability
from config.settings import Settings


def build_settings(
    *,
    sentry_dsn: str | None = None,
    langsmith_key: str | None = None,
    langsmith_project: str | None = None,
) -> Settings:
    return Settings(
        _env_file=None,
        app={"env": "test", "log_level": "DEBUG"},
        db={"host": "localhost", "name": "test", "user": "test", "password": "secret"},
        redis={"host": "localhost"},
        telegram={"bot_token": "token"},
        api={
            "two_gis_api_key": "two-gis",
            "deepseek_api_key": "deepseek",
            "sentry_dsn": sentry_dsn,
            "langsmith_api_key": langsmith_key,
            "langsmith_project": langsmith_project,
        },
    )


def test_configure_observability_initializes_enabled_integrations(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))

    configure_observability(
        build_settings(
            sentry_dsn="https://public@sentry.example/1",
            langsmith_key="secret-key",
            langsmith_project="krisha-test",
        )
    )

    assert logging.getLogger().level == logging.DEBUG
    assert calls == [
        {
            "dsn": "https://public@sentry.example/1",
            "environment": "test",
        }
    ]
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "secret-key"
    assert os.environ["LANGSMITH_PROJECT"] == "krisha-test"


def test_configure_observability_disables_incomplete_langsmith(monkeypatch) -> None:
    for name in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"):
        monkeypatch.setenv(name, "stale")
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: None)

    configure_observability(build_settings(langsmith_key="key"))

    assert "LANGSMITH_TRACING" not in os.environ
    assert "LANGSMITH_API_KEY" not in os.environ
    assert "LANGSMITH_PROJECT" not in os.environ
