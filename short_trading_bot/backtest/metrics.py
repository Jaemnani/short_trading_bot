"""Backtest performance metrics computed from the equity curve + closed trades."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(slots=True)
class BacktestMetrics:
    num_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float | None = None  # None when there are no losses
    avg_r: float | None = None
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float | None = None  # per-step (not annualized)


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else 0.0
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return max_dd * 100.0


def _sharpe(equity: list[float]) -> float | None:
    rets = [equity[i] / equity[i - 1] - 1.0 for i in range(1, len(equity)) if equity[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    return mean / std if std > 0 else None


def compute_metrics(
    starting_equity: Decimal,
    equity_curve: list[tuple[datetime, Decimal]],
    trade_pnls: list[Decimal],
    trade_rs: list[float],
) -> BacktestMetrics:
    equity = [float(e) for _, e in equity_curve]
    start = float(starting_equity)
    final = equity[-1] if equity else start

    wins = [float(p) for p in trade_pnls if p > 0]
    losses = [float(p) for p in trade_pnls if p < 0]
    gross_loss = abs(sum(losses))

    return BacktestMetrics(
        num_trades=len(trade_pnls),
        win_rate=(len(wins) / len(trade_pnls)) if trade_pnls else 0.0,
        profit_factor=(sum(wins) / gross_loss) if gross_loss > 0 else None,
        avg_r=(sum(trade_rs) / len(trade_rs)) if trade_rs else None,
        total_return_pct=((final / start - 1.0) * 100.0) if start > 0 else 0.0,
        max_drawdown_pct=_max_drawdown(equity) if equity else 0.0,
        sharpe=_sharpe(equity),
    )
