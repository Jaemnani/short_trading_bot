"""Load the trading config (watchlist + risk limits) from a JSON file.

This makes the bot configurable per operator without code changes — point `trader serve`
at a JSON file. (A per-user settings UI replaces this file source later; the engine reads
a watchlist + RiskLimits regardless of source.)

Format:
{
  "limits": {"daily_loss_limit": "500000", "max_open_positions": 5,
             "max_order_notional": "5000000", "max_ticker_exposure": "10000000"},
  "watchlist": {
    "005930": {"strategy_id": "trend_long_v1", "market": "KRX", "resolution": "1D",
               "risk_per_trade": 0.01, "strategy_params": {"require_confirm": false}}
  }
}
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..risk.limits import RiskLimits
from ..strategy.templates import StrategyTemplate


def _dec(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def load_trading_config(path: str | Path) -> tuple[dict[str, StrategyTemplate], RiskLimits]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    watchlist = {
        ticker: StrategyTemplate(**cfg) for ticker, cfg in data.get("watchlist", {}).items()
    }
    lim = data.get("limits", {})
    limits = RiskLimits(
        max_open_positions=lim.get("max_open_positions"),
        max_order_notional=_dec(lim.get("max_order_notional")),
        max_ticker_exposure=_dec(lim.get("max_ticker_exposure")),
        daily_loss_limit=_dec(lim.get("daily_loss_limit")),
        daily_loss_pct=lim.get("daily_loss_pct"),  # 권장: 자본 대비 비율 (예: 0.03)
        max_drawdown_pct=lim.get("max_drawdown_pct"),  # 총 낙폭 브레이크 (예: 0.15)
    )
    return watchlist, limits
