from decimal import Decimal

import pytest

from short_trading_bot.domain.enums import Currency, Market, PositionState, Side
from short_trading_bot.domain.factory import PositionFactory
from short_trading_bot.domain.position import IllegalTransition, PositionLot
from short_trading_bot.domain.signal import IntentKind, Signal
from short_trading_bot.strategy.algorithms.trend_long import TrendLongV1
from short_trading_bot.strategy.registry import create_strategy
from short_trading_bot.strategy.templates import StrategyTemplate

from ._helpers import make_snapshot


def _lot() -> PositionLot:
    signal = Signal(ticker="005930")
    template = StrategyTemplate(strategy_id="trend_long_v1")
    return PositionFactory.create(signal, template, lot_id="lot1")


def test_factory_builds_lot() -> None:
    lot = _lot()
    assert lot.lot_id == "lot1"
    assert lot.ticker == "005930"
    assert lot.market is Market.KRX
    assert lot.currency is Currency.KRW
    assert lot.state is PositionState.WATCHING
    assert isinstance(lot.strategy, TrendLongV1)
    assert lot.params.strategy_id == "trend_long_v1"


def test_overseas_template_sets_currency() -> None:
    lot = PositionFactory.create(
        Signal(ticker="AAPL", market=Market.NASD),
        StrategyTemplate(strategy_id="trend_long_v1", market=Market.NASD),
    )
    assert lot.market is Market.NASD
    assert lot.currency is Currency.USD


def test_buy_fill_transitions_to_holding_and_avg() -> None:
    lot = _lot()
    lot.apply_fill(Side.BUY, Decimal("10"), Decimal("100"))
    assert lot.state is PositionState.HOLDING
    assert lot.qty == Decimal("10")
    assert lot.avg_entry == Decimal("100")
    lot.apply_fill(Side.BUY, Decimal("10"), Decimal("110"))
    assert lot.avg_entry == Decimal("105")
    assert lot.qty == Decimal("20")


def test_sell_all_closes_and_realizes() -> None:
    lot = _lot()
    lot.apply_fill(Side.BUY, Decimal("10"), Decimal("100"))
    lot.apply_fill(Side.SELL, Decimal("10"), Decimal("120"))
    assert lot.state is PositionState.CLOSED
    assert lot.qty == Decimal("0")
    assert lot.realized_pnl == Decimal("200")  # (120-100)*10


def test_illegal_transition_raises() -> None:
    lot = _lot()
    lot.apply_fill(Side.BUY, Decimal("10"), Decimal("100"))
    lot.apply_fill(Side.SELL, Decimal("10"), Decimal("120"))  # -> CLOSED
    with pytest.raises(IllegalTransition):
        lot.transition_to(PositionState.HOLDING)


def test_on_bar_tracks_peak() -> None:
    lot = _lot()
    lot.apply_fill(Side.BUY, Decimal("10"), Decimal("100"))
    lot.on_bar(Decimal("105"))
    lot.on_bar(Decimal("103"))
    assert lot.peak_price == Decimal("105")
    assert lot.bars_held == 2


def test_evaluate_then_enter_sets_initial_stop() -> None:
    lot = _lot()
    prev = make_snapshot(109, macd=0.4, macd_signal=0.5, high_20=108)
    snap = make_snapshot(
        110, sma_5=109, sma_20=105, sma_60=100, adx_14=30, plus_di=30, minus_di=10,
        rsi_14=60, macd=1.0, macd_signal=0.5, macd_hist=0.5, rvol=2.0, atr_14=2.0, high_20=108,
    )
    intents = lot.evaluate(snap, Decimal("10000000"), prev=prev)
    assert intents[0].kind is IntentKind.ENTER
    # simulate the broker filling the entry
    lot.apply_fill(Side.BUY, intents[0].qty, Decimal("110"))
    assert lot.state is PositionState.HOLDING
    assert lot.initial_stop == Decimal("106.0")  # captured from the ENTER intent


def test_create_strategy_direct() -> None:
    s = create_strategy("trend_long_v1")
    assert isinstance(s, TrendLongV1)
