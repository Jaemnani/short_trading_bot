"""Risk limit configuration + the snapshot/decision types used by the RiskManager gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(slots=True)
class RiskLimits:
    """All limits optional (None = no limit). Amounts are KRW-equivalent.

    Prefer the PERCENT limits: they scale with the account, so a shrinking account
    automatically gets a tighter daily stop, and ``max_drawdown_pct`` is the total
    brake that prevents "lose the daily limit every day until the account is gone".
    """

    max_open_positions: int | None = None
    max_order_notional: Decimal | None = None
    max_ticker_exposure: Decimal | None = None
    daily_loss_limit: Decimal | None = None  # 절대액(원); block new entries at -limit
    daily_loss_pct: float | None = None  # 당일 실현손실 ≥ 자본의 X% → 신규 진입 차단
    max_drawdown_pct: float | None = None  # 피크 자본 대비 X% 하락 → 신규 진입 전면 차단


@dataclass(slots=True)
class RiskSnapshot:
    """Current portfolio state the gate evaluates against (KRW-equivalent)."""

    equity: Decimal
    open_positions: int = 0
    daily_pnl: Decimal = Decimal(0)
    ticker_exposure: dict[str, Decimal] = field(default_factory=dict)
    peak_equity: Decimal | None = None  # high-water mark for the drawdown brake


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
