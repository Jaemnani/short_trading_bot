from decimal import Decimal

from short_trading_bot.domain.enums import PositionState
from short_trading_bot.domain.params import PositionParams, TakeProfitRung
from short_trading_bot.domain.signal import IntentKind
from short_trading_bot.market.types import IndicatorSnapshot
from short_trading_bot.strategy.algorithms.trend_long import TrendLongV1
from short_trading_bot.strategy.base import StrategyContext

from ._helpers import make_snapshot

STRAT = TrendLongV1(TrendLongV1.Params())


def _ctx(
    state: PositionState,
    close: float,
    *,
    avg: float = 0.0,
    stop: float | None = None,
    peak: float = 0.0,
    tp_taken: int = 0,
    original: float = 0.0,
    qty: float = 10.0,
    bars_held: int = 0,
    max_hold: int | None = None,
    equity: str = "10000000",
    news: float | None = None,
    prev: IndicatorSnapshot | None = None,
    **ind: float,
) -> StrategyContext:
    return StrategyContext(
        snapshot=make_snapshot(close, **ind),
        state=state,
        qty=Decimal(str(qty)),
        avg_entry=Decimal(str(avg)),
        peak_price=Decimal(str(peak)),
        bars_held=bars_held,
        params=PositionParams(strategy_id="trend_long_v1", max_hold_bars=max_hold),
        equity=Decimal(equity),
        initial_stop=Decimal(str(stop)) if stop is not None else None,
        original_qty=Decimal(str(original)),
        tp_rungs_taken=tp_taken,
        prev=prev,
        news_ewma=news,
    )


def _bullish_entry_ctx(**overrides: object) -> StrategyContext:
    prev = make_snapshot(109, macd=0.4, macd_signal=0.5, high_20=108)
    base = dict(
        sma_5=109, sma_20=105, sma_60=100, adx_14=30, plus_di=30, minus_di=10,
        rsi_14=60, macd=1.0, macd_signal=0.5, macd_hist=0.5, rvol=2.0, atr_14=2.0, high_20=108,
    )
    base.update(overrides)  # type: ignore[arg-type]
    return _ctx(PositionState.WATCHING, 110, prev=prev, **base)  # type: ignore[arg-type]


def test_entry_full_signal() -> None:
    intents = STRAT.evaluate(_bullish_entry_ctx())
    assert len(intents) == 1
    it = intents[0]
    assert it.kind is IntentKind.ENTER
    assert it.qty == Decimal("25000")  # 100000 budget / (110-106)
    assert it.stop_price == Decimal("106.0")


def test_entry_blocked_by_regime() -> None:
    intents = STRAT.evaluate(_bullish_entry_ctx(adx_14=10))
    assert intents[0].kind is IntentKind.HOLD
    assert intents[0].reason == "regime_fail"


def test_entry_blocked_by_news() -> None:
    ctx = _bullish_entry_ctx()
    ctx.news_ewma = -0.5
    assert STRAT.evaluate(ctx)[0].reason == "news_block"


def test_entry_no_trigger() -> None:
    # macd already above signal (no cross), no golden, close below prior high_20
    prev = make_snapshot(109, macd=1.0, macd_signal=0.5, sma_5=109, sma_20=105, high_20=120)
    ctx = _ctx(
        PositionState.WATCHING, 110, prev=prev,
        sma_5=109, sma_20=105, sma_60=100, adx_14=30, plus_di=30, minus_di=10,
        rsi_14=60, macd=1.1, macd_signal=0.5, macd_hist=0.5, rvol=2.0, atr_14=2.0, high_20=120,
    )
    assert STRAT.evaluate(ctx)[0].reason == "no_trigger"


def test_manage_stop_loss() -> None:
    ctx = _ctx(PositionState.HOLDING, 95, avg=100, stop=96, peak=110, atr_14=2.0, sma_20=90)
    out = STRAT.evaluate(ctx)
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "stop_loss"


def test_manage_trailing_stop() -> None:
    ctx = _ctx(PositionState.HOLDING, 100, avg=90, stop=80, peak=110, atr_14=2.0, sma_20=95)
    out = STRAT.evaluate(ctx)  # trail = 110 - 3*2 = 104; close 100 <= 104
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "trailing_stop"


def test_manage_take_profit_absolute_qty() -> None:
    # risk=10, rung0 target=110; close 111 >= 110; sell 0.5 of ORIGINAL 20 = 10 shares
    params = PositionParams(
        strategy_id="trend_long_v1",
        take_profit=[TakeProfitRung(r_multiple=1.0, fraction=0.5)],
    )
    ctx = StrategyContext(
        snapshot=make_snapshot(111, atr_14=2.0, sma_20=100),
        state=PositionState.HOLDING,
        qty=Decimal("20"),
        avg_entry=Decimal("100"),
        peak_price=Decimal("111"),
        bars_held=0,
        params=params,
        equity=Decimal("10000000"),
        initial_stop=Decimal("90"),
        original_qty=Decimal("20"),
        tp_rungs_taken=0,
    )
    out = STRAT.evaluate(ctx)
    assert out[0].kind is IntentKind.TRIM
    assert out[0].reason == "take_profit_1"
    assert out[0].qty == Decimal("10")  # 0.5 of ORIGINAL 20, absolute
    assert out[0].fraction is None


def test_manage_time_stop() -> None:
    ctx = _ctx(
        PositionState.HOLDING, 108, avg=100, stop=90, peak=108, bars_held=5, max_hold=5,
        atr_14=2.0, sma_20=100,
    )
    out = STRAT.evaluate(ctx)
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "max_hold"


def test_size_floors_to_one_for_fundable_krx() -> None:
    # base_qty 1, partial confirm (factor 0.5) -> 0.5 -> floor-to-1 (don't drop fundable)
    assert STRAT._size(Decimal("1"), full_confirm=False, news_ewma=None, allow_fractional=False) == Decimal("1")
    # genuinely unaffordable (base 0) stays 0
    assert STRAT._size(Decimal("0"), full_confirm=True, news_ewma=None, allow_fractional=False) == Decimal("0")


def test_size_preserves_fractional_overseas() -> None:
    assert STRAT._size(Decimal("0.5"), full_confirm=True, news_ewma=None, allow_fractional=True) == Decimal("0.5")


def test_manage_indicator_exit() -> None:
    # close 109 < TP1 target (avg100 + 1*risk10 = 110), not stopped/trailed, but below MA20
    ctx = _ctx(PositionState.HOLDING, 109, avg=100, stop=90, peak=110, atr_14=2.0, sma_20=120)
    out = STRAT.evaluate(ctx)
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "below_ma20"


def test_manage_hold() -> None:
    ctx = _ctx(PositionState.HOLDING, 108, avg=100, stop=90, peak=108, atr_14=2.0, sma_20=100)
    out = STRAT.evaluate(ctx)
    assert out[0].kind is IntentKind.HOLD
