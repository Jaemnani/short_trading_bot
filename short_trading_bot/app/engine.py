"""Engine assembly + go-live preflight.

``build_trading_service`` wires the broker (by mode), risk, control, and news into a
TradingService. ``preflight`` returns go-live readiness checks. With no KIS keys it falls
back to the PaperBrokerAdapter so the engine still runs (paper/sim).

KIS domains: paper REST openapivts:29443 / WS ops:31000 ; live REST openapi:9443 / WS ops:21000.
Switching paper->live changes only the base URL + TR_ID prefix (handled by adapters/router).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..domain.enums import Mode
from ..execution.broker.base import BrokerAdapter
from ..execution.broker.kis import KisBrokerAdapter
from ..execution.broker.kis_overseas import KisOverseasAdapter
from ..execution.broker.paper import PaperBrokerAdapter
from ..execution.broker.routing import RoutingBrokerAdapter
from ..infra.config import Settings
from ..infra.kis_auth import KisAuth
from ..infra.logging import get_logger
from ..persistence.db import create_engine, session_factory
from ..risk.control import ControlSwitch
from ..risk.limits import RiskLimits
from ..risk.manager import RiskManager
from .service import TradingService


def kis_rest_base(mode: Mode) -> str:
    return (
        "https://openapi.koreainvestment.com:9443"
        if mode is Mode.LIVE
        else "https://openapivts.koreainvestment.com:29443"
    )


def kis_ws_url(mode: Mode) -> str:
    return "ws://ops.koreainvestment.com:21000" if mode is Mode.LIVE else "ws://ops.koreainvestment.com:31000"


def build_broker(settings: Settings, *, logger: Any = None) -> BrokerAdapter:
    log = logger or get_logger("engine")
    creds = settings.active_kis()
    if not creds.configured:
        log.warning("broker.paper_fallback", reason="KIS credentials not configured")
        return PaperBrokerAdapter()
    base = kis_rest_base(settings.mode)
    auth = KisAuth(creds, base)
    domestic = KisBrokerAdapter(auth, creds, base, settings.mode)
    overseas = KisOverseasAdapter(auth, creds, base, settings.mode)
    return RoutingBrokerAdapter(domestic, overseas)


def build_trading_service(
    settings: Settings,
    watchlist: dict[str, Any],
    *,
    limits: RiskLimits | None = None,
    control: ControlSwitch | None = None,
    notifier: Any = None,
    news_provider: Any = None,
    broker: BrokerAdapter | None = None,
) -> TradingService:
    broker = broker or build_broker(settings)
    engine = create_engine(settings.db_url)
    risk = RiskManager(limits or RiskLimits(), control or ControlSwitch())
    return TradingService(
        broker,
        session_factory(engine),
        risk,
        watchlist,
        notifier=notifier,
        news_provider=news_provider,
    )


@dataclass(slots=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str
    critical: bool = False


def preflight(settings: Settings, *, limits: RiskLimits | None = None) -> list[PreflightCheck]:
    creds = settings.active_kis()
    daily_abs = limits.daily_loss_limit if limits else None
    daily_pct = limits.daily_loss_pct if limits else None
    has_daily = (daily_abs is not None and daily_abs > Decimal(0)) or (
        daily_pct is not None and daily_pct > 0
    )
    live = settings.mode is Mode.LIVE
    return [
        PreflightCheck("mode", settings.mode in (Mode.PAPER, Mode.LIVE), settings.mode.value, True),
        PreflightCheck(
            "kis_credentials",
            creds.configured,
            "active-env keys present" if creds.configured else "missing appkey/secret/account_no",
            True,
        ),
        PreflightCheck(
            "daily_loss_limit",
            has_daily,
            f"abs={daily_abs} pct={daily_pct}",
            critical=live,
        ),
        PreflightCheck("db_persistent", ":memory:" not in settings.db_url, settings.db_url, True),
        PreflightCheck(
            "api_secret_changed",
            settings.api_jwt_secret != "dev-insecure-change-me",
            "dashboard JWT secret",
            critical=False,
        ),
        PreflightCheck(
            "live_smallest_size",
            not live,  # informational: when LIVE, remind to start small
            "start at smallest size; scale only after live matches paper" if live else "n/a",
            critical=False,
        ),
    ]


def is_ready(checks: list[PreflightCheck]) -> bool:
    return all(c.ok for c in checks if c.critical)
