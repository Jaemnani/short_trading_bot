"""ASGI entrypoint: ``uvicorn short_trading_bot.api.main:app``.

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
from .state import ApiState


def create_default_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log = get_logger("api")
    if settings.api_jwt_secret == "dev-insecure-change-me":
        log.warning("api.insecure_jwt_secret", hint="set STB_API_JWT_SECRET in prod")

    engine = create_engine(settings.db_url)
    state = ApiState(
        control=ControlSwitch(),
        session_factory=session_factory(engine),
        jwt_secret=settings.api_jwt_secret,
        username=settings.api_username,
        password=settings.api_password,
    )
    return create_app(state)


app = create_default_app()
