"""PositionParams — the immutable config frozen at lot creation.

Freezing makes each PositionLot ("주식객체") independent and reproducible. Lot-level
config lives here; algorithm-specific thresholds live in ``strategy_params`` (validated
by the chosen Strategy's ParamsModel at factory time).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import Currency, Market, Resolution


class StopConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    atr_mult: float = Field(default=2.0, gt=0)  # initial hard stop = entry - atr_mult * ATR
    chandelier_mult: float = Field(default=3.0, gt=0)  # trailing = peak - chandelier_mult * ATR
    use_trailing: bool = True
    fixed_pct: float | None = Field(default=None, gt=0, lt=1)  # optional fixed % stop instead of ATR


class TakeProfitRung(BaseModel):
    model_config = ConfigDict(frozen=True)

    r_multiple: float = Field(gt=0)  # trigger at entry + r_multiple * initial_risk
    fraction: float = Field(gt=0, le=1)  # fraction of the ORIGINAL position to sell at this rung


class PositionParams(BaseModel):
    """Frozen at lot creation; serialized to ``positions.params_json``."""

    model_config = ConfigDict(frozen=True)

    strategy_id: str
    market: Market = Market.KRX
    currency: Currency = Currency.KRW
    resolution: Resolution = Resolution.D1

    risk_per_trade: float = Field(default=0.01, gt=0, le=1.0)  # equity fraction risked to stop
    # 사이징 시 주당 리스크에 얹는 왕복 비용 여유 (손절 실손실이 예산 초과하지 않게). 0=기존.
    sizing_cost_buffer_pct: float = Field(default=0.0, ge=0, le=0.02)
    stop: StopConfig = Field(default_factory=StopConfig)
    take_profit: list[TakeProfitRung] = Field(
        default_factory=lambda: [
            TakeProfitRung(r_multiple=1.0, fraction=1 / 3),
            TakeProfitRung(r_multiple=2.0, fraction=1 / 3),
        ]
    )
    max_hold_bars: int | None = None
    tif: str = "DAY"

    # Algorithm-specific thresholds, validated by the Strategy's ParamsModel.
    strategy_params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_take_profit_total(self) -> PositionParams:
        total = sum(rung.fraction for rung in self.take_profit)
        if total > 1.0 + 1e-9:
            raise ValueError(f"take_profit fractions sum to {total:.4f} > 1.0 (would over-sell)")
        return self
