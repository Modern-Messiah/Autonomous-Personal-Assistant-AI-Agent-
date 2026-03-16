"""Tests for environment-driven settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from config.settings import Settings


def test_settings_fail_when_required_variables_missing() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_load_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "APP__ENV=dev",
                "APP__LOG_LEVEL=DEBUG",
                "DB__HOST=localhost",
                "DB__PORT=5432",
                "DB__NAME=krisha_agent",
                "DB__USER=krisha",
                "DB__PASSWORD=secret_password",
                "REDIS__HOST=localhost",
                "REDIS__PORT=6379",
                "REDIS__DB=0",
                "REDIS__PASSWORD=",
                "TELEGRAM__BOT_TOKEN=telegram_token",
                "API__TWO_GIS_API_KEY=two_gis_key",
                "API__GEMINI_API_KEY=gemini_key",
                "API__LANGSMITH_API_KEY=langsmith_key",
                "API__LANGSMITH_PROJECT=krisha-agent-dev",
                "API__SENTRY_DSN=https://public@sentry.example/1",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.app.log_level == "DEBUG"
    assert settings.db.port == 5432
    assert settings.db.name == "krisha_agent"
    assert settings.db.sqlalchemy_url.startswith("postgresql+asyncpg://")
    assert settings.redis.redis_url == "redis://localhost:6379/0"
    assert settings.scheduler.poll_interval_seconds == 60
    assert settings.scheduler.batch_size == 50
    assert settings.notion.enabled is False


def test_settings_require_notion_credentials_when_enabled(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DB__HOST=localhost",
                "DB__NAME=krisha_agent",
                "DB__USER=krisha",
                "DB__PASSWORD=secret_password",
                "REDIS__HOST=localhost",
                "TELEGRAM__BOT_TOKEN=telegram_token",
                "API__TWO_GIS_API_KEY=two_gis_key",
                "API__GEMINI_API_KEY=gemini_key",
                "API__LANGSMITH_API_KEY=langsmith_key",
                "API__LANGSMITH_PROJECT=krisha-agent-dev",
                "API__SENTRY_DSN=https://public@sentry.example/1",
                "NOTION__ENABLED=true",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        Settings(_env_file=env_file)


def test_settings_load_enabled_notion_config(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DB__HOST=localhost",
                "DB__NAME=krisha_agent",
                "DB__USER=krisha",
                "DB__PASSWORD=secret_password",
                "REDIS__HOST=localhost",
                "TELEGRAM__BOT_TOKEN=telegram_token",
                "API__TWO_GIS_API_KEY=two_gis_key",
                "API__GEMINI_API_KEY=gemini_key",
                "API__LANGSMITH_API_KEY=langsmith_key",
                "API__LANGSMITH_PROJECT=krisha-agent-dev",
                "API__SENTRY_DSN=https://public@sentry.example/1",
                "NOTION__ENABLED=true",
                "NOTION__API_TOKEN=secret_notion_token",
                "NOTION__DATABASE_ID=database-123",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.notion.enabled is True
    assert settings.notion.database_id == "database-123"
