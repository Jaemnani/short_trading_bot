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
    # 사이징 왕복비용 여유 (손절 실손실 예산 보정; PositionParams로 전달). 0=기존 동작.
    sizing_cost_buffer_pct: float = Field(default=0.0, ge=0, le=0.02)
    # 시장 레짐 필터 적용 여부: True면 시장 상태가 나쁠 때(MarketRegime) 신규 진입 차단.
    # 자체 레짐 게이트가 있는 전략(눌림목)은 False 유지 — 기존 검증 동작 불변.
    regime_filter: bool = False
    strategy_params: dict[str, Any] = Field(default_factory=dict)
