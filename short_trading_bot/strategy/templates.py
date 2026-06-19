"""StrategyTemplate — a reusable recipe binding a registered algorithm + its params
+ lot-level config (resolution, market, risk, stops, TP). The PositionFactory turns a
(Signal + StrategyTemplate) into a PositionLot.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..domain.enums import Market, Resolution
from ..domain.params import StopConfig, TakeProfitRung


class StrategyTemplate(BaseModel):
    strategy_id: str
    resolution: Resolution = Resolution.D1
    market: Market = Market.KRX
    risk_per_trade: float = Field(default=0.01, gt=0, le=1.0)
    stop: StopConfig = Field(default_factory=StopConfig)
    take_profit: list[TakeProfitRung] = Field(
        default_factory=lambda: [
            TakeProfitRung(r_multiple=1.0, fraction=1 / 3),
            TakeProfitRung(r_multiple=2.0, fraction=1 / 3),
        ]
    )
    max_hold_bars: int | None = None
    strategy_params: dict[str, Any] = Field(default_factory=dict)
