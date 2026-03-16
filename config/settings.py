"""Application settings loaded from environment variables."""

from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseModel):
    """General application settings."""

    env: str = Field(default="dev", min_length=1)
    log_level: str = Field(default="INFO", min_length=1)


class DatabaseSettings(BaseModel):
    """PostgreSQL settings."""

    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr

    @property
    def sqlalchemy_url(self) -> str:
        safe_password = quote_plus(self.password.get_secret_value())
        return f"postgresql+asyncpg://{self.user}:{safe_password}@{self.host}:{self.port}/{self.name}"

    @property
    def psycopg_url(self) -> str:
        safe_password = quote_plus(self.password.get_secret_value())
        return f"postgresql://{self.user}:{safe_password}@{self.host}:{self.port}/{self.name}"


class RedisSettings(BaseModel):
    """Redis settings."""

    host: str = Field(min_length=1)
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0)
    password: SecretStr | None = None

    @property
    def redis_url(self) -> str:
        if self.password is None or not self.password.get_secret_value():
            return f"redis://{self.host}:{self.port}/{self.db}"
        safe_password = quote_plus(self.password.get_secret_value())
        return f"redis://:{safe_password}@{self.host}:{self.port}/{self.db}"


class TelegramSettings(BaseModel):
    """Telegram bot settings."""

    bot_token: SecretStr


class APISettings(BaseModel):
    """External integrations keys."""

    two_gis_api_key: SecretStr
    gemini_api_key: SecretStr
    langsmith_api_key: SecretStr
    langsmith_project: str = Field(min_length=1)
    sentry_dsn: str = Field(min_length=1)


class ParserSettings(BaseModel):
    """Parser behavior settings."""

    min_delay_seconds: float = Field(default=1.0, ge=0, le=30)
    max_delay_seconds: float = Field(default=3.0, ge=0, le=30)
    timeout_ms: int = Field(default=30_000, ge=1_000, le=120_000)
    dedup_ttl_seconds: int = Field(default=86_400, ge=60)

    @model_validator(mode="after")
    def validate_delay_range(self) -> "ParserSettings":
        if self.min_delay_seconds > self.max_delay_seconds:
            msg = "min_delay_seconds cannot be greater than max_delay_seconds"
            raise ValueError(msg)
        return self


class ScoringSettings(BaseModel):
    """LLM scoring behavior settings."""

    model: str = Field(default="gemini-2.5-flash", min_length=1)
    temperature: float = Field(default=0.2, ge=0, le=1)
    timeout_seconds: float = Field(default=15.0, gt=0, le=120)


class SchedulerSettings(BaseModel):
    """Background scheduler behavior settings."""

    runtime: Literal["inline", "arq"] = "inline"
    poll_interval_seconds: int = Field(default=60, ge=1, le=3600)
    batch_size: int = Field(default=50, ge=1, le=1000)


class ArqSettings(BaseModel):
    """ARQ queue settings for background worker mode."""

    queue_name: str = Field(default="krisha:monitor", min_length=1)
    job_timeout_seconds: int = Field(default=900, ge=1, le=86_400)
    max_tries: int = Field(default=3, ge=1, le=100)


class NotionSettings(BaseModel):
    """Optional Notion sync settings."""

    enabled: bool = False
    api_token: SecretStr | None = None
    database_id: str | None = Field(default=None, min_length=1)
    timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    @model_validator(mode="after")
    def validate_enabled_contract(self) -> "NotionSettings":
        if not self.enabled:
            return self
        if self.api_token is None or not self.api_token.get_secret_value():
            msg = "api_token is required when notion sync is enabled"
            raise ValueError(msg)
        if self.database_id is None:
            msg = "database_id is required when notion sync is enabled"
            raise ValueError(msg)
        return self


class Settings(BaseSettings):
    """Root settings object loaded from .env and process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    app: AppSettings = Field(default_factory=AppSettings)
    db: DatabaseSettings
    redis: RedisSettings
    telegram: TelegramSettings
    api: APISettings
    parser: ParserSettings = Field(default_factory=ParserSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    arq: ArqSettings = Field(default_factory=ArqSettings)
    notion: NotionSettings = Field(default_factory=NotionSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()  # type: ignore[call-arg]
