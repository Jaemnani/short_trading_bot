"""ASGI entrypoint: ``uvicorn --factory short_trading_bot.api.main:create_default_app``.

Builds an ApiState from settings (env/.env). In deployment the same ControlSwitch and DB
session factory are shared with the running TradingService so dashboard controls reach the
live engine.
"""

from __future__ import annotations

from fastapi import FastAPI

from ..infra.config import get_settings
from ..infra.logging import configure_logging, get_logger
from ..persistence.db import create_engine, session_factory
from ..risk.control import ControlSwitch
from .app import create_app
from .security import insecure_api_config, is_loopback_host
from .state import ApiState


class InsecureApiConfig(RuntimeError):
    """기본 자격증명으로 외부 인터페이스에 API 를 열려고 할 때."""


def create_default_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log = get_logger("api")
    problems = insecure_api_config(settings.api_jwt_secret, settings.api_password)
    if problems:
        if not is_loopback_host(settings.api_host):
            # 공개된 기본값으로 LAN/인터넷에 열면 누구나 토큰을 위조해 전량청산을 누른다.
            log.error("api.insecure_config_refused", host=settings.api_host, problems=problems)
            raise InsecureApiConfig(
                "refusing to serve the dashboard on "
                f"{settings.api_host} with insecure credentials: {'; '.join(problems)}. "
                "Set STB_API_JWT_SECRET (openssl rand -hex 32) and STB_API_PASSWORD in .env, "
                "or bind to 127.0.0.1."
            )
        log.warning("api.insecure_config_loopback_only", problems=problems)

    engine = create_engine(settings.db_url)
    state = ApiState(
        control=ControlSwitch(),
        session_factory=session_factory(engine),
        jwt_secret=settings.api_jwt_secret,
        username=settings.api_username,
        password=settings.api_password,
        cors_origins=list(settings.api_cors_origins),
    )
    return create_app(state)


def __getattr__(name: str) -> FastAPI:
    # ``uvicorn short_trading_bot.api.main:app`` 호환 — 모듈 import 만으로 앱(과 보안 검사)이
    # 돌지 않게 지연 생성한다. `trader api` 는 factory 로 직접 부른다.
    if name == "app":
        return create_default_app()
    raise AttributeError(name)
