"""Risk limit configuration + the snapshot/decision types used by the RiskManager gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(slots=True)
class RiskLimits:
    """All limits optional (None = no limit). Amounts are KRW-equivalent."""

    max_open_positions: int | None = None
    max_order_notional: Decimal | None = None
    max_ticker_exposure: Decimal | None = None
    daily_loss_limit: Decimal | None = None  # positive; block new entries when daily_pnl <= -limit


@dataclass(slots=True)
class RiskSnapshot:
    """Current portfolio state the gate evaluates against (KRW-equivalent)."""

    equity: Decimal
    open_positions: int = 0
    daily_pnl: Decimal = Decimal(0)
    ticker_exposure: dict[str, Decimal] = field(default_factory=dict)


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str = "ok"

    @classmethod
    def allow(cls) -> RiskDecision:
        return cls(True, "ok")

    @classmethod
    def block(cls, reason: str) -> RiskDecision:
        return cls(False, reason)
