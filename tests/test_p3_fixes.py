"""Regression tests for fixes from the P3 adversarial review."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from short_trading_bot.domain.enums import PositionState, Resolution, Side
from short_trading_bot.domain.factory import PositionFactory
from short_trading_bot.domain.params import PositionParams, StopConfig, TakeProfitRung
from short_trading_bot.domain.position import IllegalTransition, PositionLot
from short_trading_bot.domain.signal import IntentKind, Signal
from short_trading_bot.market.indicators import IndicatorEngine
from short_trading_bot.market.types import Bar
from short_trading_bot.strategy.base import Strategy, StrategyContext, StrategyMeta
from short_trading_bot.strategy.registry import all_strategies, register_strategy
from short_trading_bot.strategy.templates import StrategyTemplate

from ._helpers import make_snapshot


def _holding_lot(**over: object) -> PositionLot:
    lot = PositionFactory.create(Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1"))
    lot.state = PositionState.HOLDING
    lot.qty = Decimal("30")
    lot.avg_entry = Decimal("100")
    lot.initial_stop = Decimal("90")
    lot.original_qty = Decimal("30")
    lot.peak_price = Decimal("125")
    for k, v in over.items():
        setattr(lot, k, v)
    return lot


# --- over-sell PnL clamp ---

def test_oversell_does_not_fabricate_pnl() -> None:
    lot = PositionFactory.create(Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1"))
    lot.apply_fill(Side.BUY, Decimal("10"), Decimal("100"))
    lot.apply_fill(Side.SELL, Decimal("15"), Decimal("120"))  # over-sell 5 phantom shares
    assert lot.qty == Decimal("0")
    assert lot.state is PositionState.CLOSED
    assert lot.realized_pnl == Decimal("200")  # (120-100)*10, NOT *15


# --- TP ladder advances and never re-fires the same rung ---

def test_tp_ladder_advances() -> None:
    lot = _holding_lot()
    snap = make_snapshot(125, atr_14=2.0, sma_20=100)
    first = lot.evaluate(snap, Decimal("10000000"))
    assert first[0].reason == "take_profit_1" and lot.tp_rungs_taken == 1
    second = lot.evaluate(snap, Decimal("10000000"))
    assert second[0].reason == "take_profit_2" and lot.tp_rungs_taken == 2
    third = lot.evaluate(snap, Decimal("10000000"))
    assert third[0].kind is IntentKind.HOLD  # ladder exhausted, no re-fire


def test_warmup_guard() -> None:
    lot = PositionFactory.create(Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1"))
    out = lot.evaluate(make_snapshot(100, bar_count=10), Decimal("10000000"))
    assert out[0].reason == "warming_up"


# --- state machine honesty ---

def test_add_fill_enters_scaling() -> None:
    lot = PositionFactory.create(Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1"))
    lot.apply_fill(Side.BUY, Decimal("10"), Decimal("100"))
    lot.apply_fill(Side.BUY, Decimal("5"), Decimal("110"), is_add=True)
    assert lot.state is PositionState.SCALING
    assert lot.qty == Decimal("15")


def test_begin_exit_and_error_recovery() -> None:
    lot = PositionFactory.create(Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1"))
    lot.apply_fill(Side.BUY, Decimal("10"), Decimal("100"))
    lot.begin_exit()
    assert lot.state is PositionState.EXITING
    lot.mark_error()
    assert lot.state is PositionState.ERROR
    lot.recover(PositionState.HOLDING)
    assert lot.state is PositionState.HOLDING


def test_cannot_close_with_open_qty() -> None:
    lot = PositionFactory.create(Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1"))
    lot.apply_fill(Side.BUY, Decimal("10"), Decimal("100"))
    with pytest.raises(IllegalTransition):
        lot.transition_to(PositionState.CLOSED)


# --- param validation ---

def test_takeprofit_fraction_bounds() -> None:
    with pytest.raises(ValidationError):
        TakeProfitRung(r_multiple=1.0, fraction=1.5)


def test_takeprofit_sum_capped() -> None:
    with pytest.raises(ValidationError):
        PositionParams(
            strategy_id="trend_long_v1",
            take_profit=[
                TakeProfitRung(r_multiple=1.0, fraction=0.6),
                TakeProfitRung(r_multiple=2.0, fraction=0.6),
            ],
        )


def test_risk_per_trade_bounds() -> None:
    with pytest.raises(ValidationError):
        PositionParams(strategy_id="trend_long_v1", risk_per_trade=-0.1)


def test_fixed_pct_bounds() -> None:
    with pytest.raises(ValidationError):
        StopConfig(fixed_pct=1.5)


# --- plugin contract: auto-discovery + supported_resolutions ---

class _EmptyParams(BaseModel):
    pass


@register_strategy("res_d1_only")
class _D1Only(Strategy):
    meta = StrategyMeta(id="res_d1_only", name="D1 only", supported_resolutions=[Resolution.D1])
    ParamsModel = _EmptyParams

    def evaluate(self, ctx: StrategyContext) -> list:
        return []


def test_plugin_autodiscovered() -> None:
    # trend_long_v1 self-registered via pkgutil discovery on package import (no manual edit)
    assert "trend_long_v1" in all_strategies()


def test_unsupported_resolution_rejected() -> None:
    with pytest.raises(ValueError):
        PositionFactory.create(
            Signal(ticker="005930"),
            StrategyTemplate(strategy_id="res_d1_only", resolution=Resolution.M1),
        )
    # supported resolution is accepted
    lot = PositionFactory.create(
        Signal(ticker="005930"),
        StrategyTemplate(strategy_id="res_d1_only", resolution=Resolution.D1),
    )
    assert lot.params.resolution is Resolution.D1


# --- incremental OBV + session-anchored VWAP ---

def _bar(close: float, ts: datetime, vol: float = 10.0) -> Bar:
    c = Decimal(str(close))
    return Bar(
        ticker="005930", resolution=Resolution.M1, ts=ts,
        open=c, high=c, low=c, close=c, volume=Decimal(str(vol)), value=c * Decimal(str(vol)),
    )


def test_obv_is_cumulative() -> None:
    eng = IndicatorEngine()
    eng.update(_bar(100, datetime(2026, 1, 2, 9, 0, tzinfo=UTC)))
    eng.update(_bar(101, datetime(2026, 1, 2, 9, 1, tzinfo=UTC)))
    snap = eng.update(_bar(102, datetime(2026, 1, 2, 9, 2, tzinfo=UTC)))
    assert snap.get("obv") == 20.0  # two up-bars * vol 10


def test_vwap_session_anchored_despite_small_window() -> None:
    eng = IndicatorEngine(window=2)  # tiny window must NOT truncate the session VWAP
    base = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    snap = None
    for i, close in enumerate([10, 20, 30, 40, 50]):
        snap = eng.update(_bar(close, base + timedelta(minutes=i), vol=1))
    assert snap is not None
    assert snap.get("vwap") == pytest.approx(30.0)  # full session mean, not last-2


def test_vwap_resets_on_new_day() -> None:
    eng = IndicatorEngine()
    eng.update(_bar(100, datetime(2026, 1, 2, 9, 0, tzinfo=UTC)))
    snap = eng.update(_bar(200, datetime(2026, 1, 3, 9, 0, tzinfo=UTC)))  # new session
    assert snap.get("vwap") == pytest.approx(200.0)
