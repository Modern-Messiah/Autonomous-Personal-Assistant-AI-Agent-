"""Application settings loaded from environment variables."""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, SecretStr
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()  # type: ignore[call-arg]
