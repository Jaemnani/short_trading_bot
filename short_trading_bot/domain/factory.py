"""PositionFactory — builds a PositionLot from (Signal + StrategyTemplate).

This is the concrete realization of "구입마다 동적 생성되는 주식객체": params are frozen
and the selected algorithm is instantiated and bound to the lot.
"""

from __future__ import annotations

from uuid import uuid4

import short_trading_bot.strategy.algorithms  # noqa: F401  (ensures plugins are registered)

from ..strategy.registry import create_strategy
from ..strategy.templates import StrategyTemplate
from .params import PositionParams
from .position import PositionLot
from .signal import Signal


class PositionFactory:
    @staticmethod
    def create(
        signal: Signal, template: StrategyTemplate, *, lot_id: str | None = None
    ) -> PositionLot:
        market = template.market
        params = PositionParams(
            strategy_id=template.strategy_id,
            market=market,
            currency=market.currency,
            resolution=template.resolution,
            risk_per_trade=template.risk_per_trade,
            stop=template.stop,
            take_profit=template.take_profit,
            max_hold_bars=template.max_hold_bars,
            strategy_params=template.strategy_params,
        )
        strategy = create_strategy(template.strategy_id, template.strategy_params)
        supported = strategy.meta.supported_resolutions
        if supported is not None and template.resolution not in supported:
            raise ValueError(
                f"strategy '{template.strategy_id}' does not support resolution "
                f"{template.resolution.value} (supported: {[r.value for r in supported]})"
            )
        return PositionLot(
            lot_id=lot_id or uuid4().hex,
            ticker=signal.ticker,
            market=market,
            currency=market.currency,
            params=params,
            strategy=strategy,
        )
