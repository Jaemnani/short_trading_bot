"""pullback_daily_v1 (눌림목 매수) behavior tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from short_trading_bot.domain.enums import PositionState, Resolution
from short_trading_bot.domain.factory import PositionFactory
from short_trading_bot.domain.params import PositionParams
from short_trading_bot.domain.signal import IntentKind, Signal
from short_trading_bot.market.types import IndicatorSnapshot
from short_trading_bot.strategy.algorithms.pullback_daily import PullbackDaily, PullbackParams
from short_trading_bot.strategy.base import StrategyContext
from short_trading_bot.strategy.templates import StrategyTemplate

from ._helpers import make_snapshot

PARAMS = PositionParams(strategy_id="pullback_daily_v1", resolution=Resolution.D1)
STRAT = PullbackDaily(PullbackParams())

# Healthy uptrend context: MA20=100 > MA60=90.
UPTREND = dict(sma_20=100.0, sma_60=90.0, atr_14=2.0)


def _ctx(
    state: PositionState,
    close: float,
    *,
    low: float | None = None,
    prev_close: float | None = None,
    qty: float = 0.0,
    avg: float = 0.0,
    stop: float | None = None,
    peak: float = 0.0,
    original: float = 0.0,
    bars_held: int = 0,
    news: float | None = None,
    **ind: float,
) -> StrategyContext:
    prev: IndicatorSnapshot | None = None
    if prev_close is not None:
        prev = make_snapshot(prev_close, resolution=Resolution.D1)
    return StrategyContext(
        snapshot=make_snapshot(close, resolution=Resolution.D1, low=low, **ind),
        state=state,
        qty=Decimal(str(qty)),
        avg_entry=Decimal(str(avg)),
        peak_price=Decimal(str(peak)),
        bars_held=bars_held,
        params=PARAMS,
        equity=Decimal("10000000"),
        initial_stop=Decimal(str(stop)) if stop is not None else None,
        original_qty=Decimal(str(original)),
        prev=prev,
        news_ewma=news,
    )


def test_pullback_entry() -> None:
    # low dipped to MA20(100), closed back up at 102 > prev 100.5, RSI 48
    out = STRAT.evaluate(
        _ctx(PositionState.WATCHING, 102, low=99.8, prev_close=100.5, rsi_14=48.0, **UPTREND)
    )
    intent = out[0]
    assert intent.kind is IntentKind.ENTER and intent.reason == "pullback_buy"
    assert intent.qty is not None and intent.qty > 0
    assert intent.stop_price == Decimal("98")  # 102 - 2*ATR(2.0)


def test_no_entry_without_uptrend() -> None:
    out = STRAT.evaluate(
        _ctx(PositionState.WATCHING, 89, low=88, prev_close=88.5, rsi_14=48.0,
             sma_20=95.0, sma_60=90.0, atr_14=2.0)  # close < MA60
    )
    assert out[0].reason == "no_uptrend"


def test_no_entry_without_pullback() -> None:
    out = STRAT.evaluate(  # price riding high, never touched MA20
        _ctx(PositionState.WATCHING, 112, low=110, prev_close=111, rsi_14=55.0, **UPTREND)
    )
    assert out[0].reason == "no_pullback"


def test_no_entry_when_ma20_broken() -> None:
    out = STRAT.evaluate(  # closed >2% below MA20 — pullback failed
        _ctx(PositionState.WATCHING, 97.5, low=96, prev_close=97, rsi_14=42.0, **UPTREND)
    )
    assert out[0].reason == "broke_ma20"


def test_no_entry_outside_rsi_zone_or_without_turn_up() -> None:
    weak = STRAT.evaluate(
        _ctx(PositionState.WATCHING, 102, low=99.8, prev_close=100.5, rsi_14=30.0, **UPTREND)
    )
    assert weak[0].reason == "rsi_out_of_zone"
    falling = STRAT.evaluate(
        _ctx(PositionState.WATCHING, 100.2, low=99.8, prev_close=101.0, rsi_14=48.0, **UPTREND)
    )
    assert falling[0].reason == "no_turn_up"


def test_manage_exits() -> None:
    stopped = STRAT.evaluate(
        _ctx(PositionState.HOLDING, 97.9, qty=10, avg=102, stop=98, peak=103, **UPTREND)
    )
    assert stopped[0].kind is IntentKind.EXIT and stopped[0].reason == "hard_stop"

    trend_break = STRAT.evaluate(  # close below MA60, no other exit armed
        _ctx(PositionState.HOLDING, 89, qty=10, avg=102, stop=80, peak=103,
             sma_20=95.0, sma_60=90.0, atr_14=20.0)
    )
    assert trend_break[0].reason == "trend_break"


def test_take_profit_ladder() -> None:
    # risk = 102-98 = 4 → rung0 target 106; close 107 → TRIM 1/3 of original 12
    out = STRAT.evaluate(
        _ctx(PositionState.HOLDING, 107, qty=12, avg=102, stop=98, peak=107, original=12,
             sma_20=100.0, sma_60=90.0, atr_14=10.0)
    )
    assert out[0].kind is IntentKind.TRIM and out[0].reason == "take_profit_1"
    assert out[0].qty == Decimal("3")  # floor(12 * float(1/3))


def test_daily_only_contract_and_registry() -> None:
    from short_trading_bot.strategy.registry import all_strategies

    assert "pullback_daily_v1" in all_strategies()
    with pytest.raises(ValueError):
        PositionFactory.create(
            Signal(ticker="005930"),
            StrategyTemplate(strategy_id="pullback_daily_v1", resolution=Resolution.M5),
        )
    lot = PositionFactory.create(
        Signal(ticker="005930"),
        StrategyTemplate(strategy_id="pullback_daily_v1", resolution=Resolution.D1),
    )
    assert lot.params.resolution is Resolution.D1
