"""Layered application configuration.

Resolution order (highest wins): environment variables -> ``.env`` file -> code defaults.
Settings are prefixed ``STB_`` and nested with ``__`` (e.g. ``STB_KIS__PAPER__APP_KEY``).

Secrets (API keys) live only in env / ``.env`` (gitignored) — never in the DB, code, or logs.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..domain.enums import Mode


class KisEnvCreds(BaseModel):
    """KIS Developers credentials for a single environment (paper or live)."""

    app_key: str = ""
    app_secret: str = ""
    account_no: str = ""
    account_product_code: str = "01"

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret and self.account_no)


class KisSettings(BaseModel):
    paper: KisEnvCreds = Field(default_factory=KisEnvCreds)
    live: KisEnvCreds = Field(default_factory=KisEnvCreds)


class NotifierSettings(BaseModel):
    discord_webhook_url: str = ""  # Discord 채널 웹후크 URL (권장)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STB_",
        env_nested_delimiter="__",
        env_file=(".env", ".env.local"),  # .env.local overrides .env; both gitignored
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: Mode = Mode.PAPER
    dry_run: bool = True
    db_url: str = "sqlite+aiosqlite:///./data/short_trading_bot.db"
    log_level: str = "INFO"
    log_format: str = "console"  # "console" | "json"

    kis: KisSettings = Field(default_factory=KisSettings)
    dart_api_key: str = ""
    notifier: NotifierSettings = Field(default_factory=NotifierSettings)

    # API / dashboard (P10). Override in prod; serve behind HTTPS.
    api_jwt_secret: str = "dev-insecure-change-me"
    api_username: str = "admin"
    api_password: str = "admin"

    @property
    def is_live(self) -> bool:
        return self.mode is Mode.LIVE

    def active_kis(self) -> KisEnvCreds:
        """Credentials for the currently selected mode (LIVE uses live, else paper)."""
        return self.kis.live if self.is_live else self.kis.paper


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings. Tests may pass an explicit ``Settings`` instead."""
    return Settings()
