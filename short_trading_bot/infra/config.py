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
    discord_webhook_url: str = ""  # Discord 채널 웹후크 URL
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # 카카오톡 나에게 보내기 (권장 — 모바일 상시 채널). 키 설정 + `trader kakao-auth` 1회.
    kakao_rest_api_key: str = ""  # developers.kakao.com 앱의 REST API 키
    # 앱의 [보안] > Client Secret 이 '사용함' 이면 필수 — 없으면 토큰 발급/갱신이
    # KOE010 "Bad client credentials" 로 거부된다. 사용 안 함이면 빈 값 유지.
    kakao_client_secret: str = ""
    kakao_token_path: str = "data/kakao_token.json"


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
    # 해외 거래/조회 사용 여부. False(기본) = 해외 어댑터를 아예 만들지 않아 해외 API를
    # 한 번도 호출하지 않는다 — 한국 집중 방침 + 모의 도메인 해외 TR 간헐 500이
    # 엔진을 죽였던 사고(2026-08-03)의 원천 차단. 미국 확장 재개 시 STB_OVERSEAS_ENABLED=true.
    overseas_enabled: bool = False
    dart_api_key: str = ""
    notifier: NotifierSettings = Field(default_factory=NotifierSettings)

    # API / dashboard (P10). Override in prod; serve behind HTTPS.
    # 기본값(공개된 값)이 남아 있으면 loopback 이 아닌 주소로는 API 가 기동을 거부한다
    # (api/security.py insecure_api_config) — 대시보드는 실계좌 전량청산을 누를 수 있다.
    api_jwt_secret: str = "dev-insecure-change-me"
    api_username: str = "admin"
    api_password: str = "admin"
    api_host: str = "0.0.0.0"  # `trader api --host` 가 덮어쓴다
    # 대시보드는 같은 오리진(/)에서 서빙되므로 CORS 불필요. 개발 서버(vite) 등 다른
    # 오리진이 필요할 때만 명시 목록으로 허용한다. "*" 는 무시된다.
    api_cors_origins: list[str] = Field(default_factory=list)

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
