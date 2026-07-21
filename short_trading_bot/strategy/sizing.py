"""Risk-based position sizing and stop helpers (reused by all algorithms)."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def _d(x: float | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def risk_based_qty(
    equity: Decimal,
    risk_per_trade: float,
    entry: Decimal,
    stop: Decimal,
    *,
    allow_fractional: bool = False,
    max_notional_pct: float = 0.95,
    cost_buffer_pct: float = 0.0,
) -> Decimal:
    """shares = (equity * risk_per_trade) / (entry - stop); 0 if inputs invalid.

    Capped so notional never exceeds ``max_notional_pct`` of equity — a tight stop
    otherwise produces an order larger than the account (silently unfillable).

    ``cost_buffer_pct``: 손절 시 실손실이 예산을 넘지 않도록 주당 리스크에 왕복
    비용(슬리피지+수수료+거래세)을 얹는다 — 검증에서 hard_stop 평균이 예산 대비
    +32% 초과했던 원인 보정. 0 = 기존 동작.
    """
    if entry <= 0 or stop <= 0 or entry <= stop:
        return Decimal(0)
    budget = equity * _d(risk_per_trade)
    per_share = (entry - stop) + entry * _d(cost_buffer_pct)
    raw = budget / per_share
    cap = equity * _d(max_notional_pct) / entry  # 자본 상한 캡
    raw = min(raw, cap)
    if allow_fractional:
        return raw
    return raw.to_integral_value(rounding=ROUND_DOWN)


def atr_stop(entry: Decimal, atr: float | Decimal, mult: float) -> Decimal:
    return entry - _d(mult) * _d(atr)


def chandelier_stop(peak: Decimal, atr: float | Decimal, mult: float) -> Decimal:
    """Trailing stop = highest price since entry - mult * ATR (ratchets up only)."""
    return peak - _d(mult) * _d(atr)


def pct_stop(entry: Decimal, pct: float) -> Decimal:
    return entry * (Decimal(1) - _d(pct))
