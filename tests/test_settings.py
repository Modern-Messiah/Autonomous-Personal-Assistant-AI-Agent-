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
                "API__DEEPSEEK_API_KEY=deepseek_key",
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
    assert settings.scheduler.runtime == "inline"
    assert settings.scheduler.poll_interval_seconds == 60
    assert settings.scheduler.batch_size == 50
    assert settings.arq.queue_name == "krisha:monitor"
    assert settings.arq.job_timeout_seconds == 900
    assert settings.arq.max_tries == 3
    assert settings.notion.enabled is False


def test_settings_allow_empty_notion_credentials_when_disabled(tmp_path: Path) -> None:
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
                "API__DEEPSEEK_API_KEY=deepseek_key",
                "API__LANGSMITH_API_KEY=langsmith_key",
                "API__LANGSMITH_PROJECT=krisha-agent-dev",
                "API__SENTRY_DSN=https://public@sentry.example/1",
                "NOTION__ENABLED=false",
                "NOTION__API_TOKEN=",
                "NOTION__DATABASE_ID=",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.notion.api_token is None
    assert settings.notion.database_id is None


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
                "API__DEEPSEEK_API_KEY=deepseek_key",
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
                "API__DEEPSEEK_API_KEY=deepseek_key",
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


def test_observability_integrations_are_optional(tmp_path: Path) -> None:
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
                "API__DEEPSEEK_API_KEY=deepseek_key",
                "API__LANGSMITH_API_KEY=",
                "API__LANGSMITH_PROJECT=",
                "API__SENTRY_DSN=",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.api.langsmith_api_key is None
    assert settings.api.langsmith_project is None
    assert settings.api.sentry_dsn is None


def test_log_level_is_normalized_and_validated(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "APP__LOG_LEVEL=debug",
                "DB__HOST=localhost",
                "DB__NAME=krisha_agent",
                "DB__USER=krisha",
                "DB__PASSWORD=secret_password",
                "REDIS__HOST=localhost",
                "TELEGRAM__BOT_TOKEN=telegram_token",
                "API__TWO_GIS_API_KEY=two_gis_key",
                "API__DEEPSEEK_API_KEY=deepseek_key",
            ]
        ),
        encoding="utf-8",
    )
    assert Settings(_env_file=env_file).app.log_level == "DEBUG"

    env_file.write_text(env_file.read_text().replace("debug", "verbose"), encoding="utf-8")
    with pytest.raises(ValidationError):
        Settings(_env_file=env_file)
