"""orb_intraday_v1 (시가돌파 단타) behavior tests."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from short_trading_bot.domain.enums import PositionState, Resolution
from short_trading_bot.domain.factory import PositionFactory
from short_trading_bot.domain.params import PositionParams
from short_trading_bot.domain.signal import IntentKind, Signal
from short_trading_bot.strategy.algorithms.orb_intraday import KST, OrbIntraday, OrbParams
from short_trading_bot.strategy.base import StrategyContext
from short_trading_bot.strategy.templates import StrategyTemplate

from ._helpers import make_snapshot

PARAMS = PositionParams(strategy_id="orb_intraday_v1", resolution=Resolution.M5)


def _ts(hh: int, mm: int, day: int = 6) -> datetime:
    return datetime(2026, 7, day, hh, mm, tzinfo=KST)  # 2026-07-06 = Monday


def _ctx(
    strat: OrbIntraday,
    state: PositionState,
    close: float,
    hh: int,
    mm: int,
    *,
    high: float | None = None,
    low: float | None = None,
    day: int = 6,
    qty: float = 0.0,
    avg: float = 0.0,
    stop: float | None = None,
    peak: float = 0.0,
    original: float = 0.0,
    news: float | None = None,
    **ind: float,
) -> StrategyContext:
    return StrategyContext(
        snapshot=make_snapshot(
            close, resolution=Resolution.M5, ts=_ts(hh, mm, day), bar_count=100,
            high=high, low=low, **ind,
        ),
        state=state,
        qty=Decimal(str(qty)),
        avg_entry=Decimal(str(avg)),
        peak_price=Decimal(str(peak)),
        bars_held=0,
        params=PARAMS,
        equity=Decimal("10000000"),
        initial_stop=Decimal(str(stop)) if stop is not None else None,
        original_qty=Decimal(str(original)),
        news_ewma=news,
    )


def _warmed(strat: OrbIntraday) -> None:
    """Feed the 09:00~09:25 opening-range bars (OR = high 105 / low 99)."""
    for mm, hi, lo in [(0, 102, 99), (5, 104, 100), (10, 105, 101), (15, 103, 100), (20, 104, 101), (25, 105, 102)]:
        strat.evaluate(_ctx(strat, PositionState.WATCHING, hi - 1, 9, mm, high=hi, low=lo))


def test_no_entry_while_or_forming() -> None:
    strat = OrbIntraday(OrbParams())
    out = strat.evaluate(_ctx(strat, PositionState.WATCHING, 104, 9, 15, high=105, low=100))
    assert out[0].kind is IntentKind.HOLD and out[0].reason == "or_forming"


def test_breakout_enters_with_stop() -> None:
    strat = OrbIntraday(OrbParams())
    _warmed(strat)
    out = strat.evaluate(
        _ctx(strat, PositionState.WATCHING, 106, 9, 35, high=106.5, low=104.5, rvol=2.0, vwap=103.0, atr_14=1.0)
    )
    intent = out[0]
    assert intent.kind is IntentKind.ENTER and intent.reason == "orb_breakout"
    assert intent.qty is not None and intent.qty > 0
    # stop = max(OR low 99, 106 - 1.5*1.0 = 104.5) = 104.5
    assert intent.stop_price == Decimal("104.5")


def test_breakout_blocked_by_rvol_and_vwap() -> None:
    strat = OrbIntraday(OrbParams())
    _warmed(strat)
    low_vol = strat.evaluate(
        _ctx(strat, PositionState.WATCHING, 106, 9, 35, rvol=0.5, vwap=103.0, atr_14=1.0)
    )
    assert low_vol[0].reason == "rvol_low"
    below_vwap = strat.evaluate(
        _ctx(strat, PositionState.WATCHING, 106, 9, 35, rvol=2.0, vwap=107.0, atr_14=1.0)
    )
    assert below_vwap[0].reason == "below_vwap"


def test_no_breakout_below_or_high() -> None:
    strat = OrbIntraday(OrbParams())
    _warmed(strat)
    out = strat.evaluate(_ctx(strat, PositionState.WATCHING, 104, 10, 0, rvol=2.0, atr_14=1.0))
    assert out[0].reason == "below_or_high"


def test_one_entry_per_day_and_next_day_reset() -> None:
    strat = OrbIntraday(OrbParams())
    _warmed(strat)
    first = strat.evaluate(
        _ctx(strat, PositionState.WATCHING, 106, 9, 40, rvol=2.0, vwap=103.0, atr_14=1.0)
    )
    assert first[0].kind is IntentKind.ENTER
    again = strat.evaluate(
        _ctx(strat, PositionState.WATCHING, 107, 10, 0, rvol=2.0, vwap=103.0, atr_14=1.0)
    )
    assert again[0].reason == "daily_entry_limit"
    # next trading day: OR resets, no stale range
    next_day = strat.evaluate(
        _ctx(strat, PositionState.WATCHING, 110, 9, 40, day=7, rvol=2.0, atr_14=1.0)
    )
    assert next_day[0].reason == "or_forming"


def test_entry_cutoff() -> None:
    strat = OrbIntraday(OrbParams())
    _warmed(strat)
    out = strat.evaluate(
        _ctx(strat, PositionState.WATCHING, 106, 14, 30, rvol=2.0, vwap=103.0, atr_14=1.0)
    )
    assert out[0].reason == "entry_cutoff"


def test_session_end_flattens_everything() -> None:
    strat = OrbIntraday(OrbParams())
    _warmed(strat)
    out = strat.evaluate(
        _ctx(strat, PositionState.HOLDING, 108, 15, 10, qty=10, avg=106, stop=104.5, peak=109, atr_14=1.0)
    )
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "session_end"


def test_hard_stop_and_take_profit() -> None:
    strat = OrbIntraday(OrbParams())
    _warmed(strat)
    stopped = strat.evaluate(
        _ctx(strat, PositionState.HOLDING, 104.0, 10, 0, qty=10, avg=106, stop=104.5, peak=107, atr_14=1.0)
    )
    assert stopped[0].kind is IntentKind.EXIT and stopped[0].reason == "hard_stop"

    # risk = 106 - 104.5 = 1.5; rung0 target = 106 + 1.5 = 107.5
    tp = strat.evaluate(
        _ctx(strat, PositionState.HOLDING, 108.0, 10, 30, qty=12, avg=106, stop=104.5,
             peak=108, original=12, atr_14=0.5)
    )
    assert tp[0].kind is IntentKind.TRIM and tp[0].reason == "take_profit_1"
    assert tp[0].qty == Decimal("3")  # floor(12 * float(1/3)) — conservative ROUND_DOWN


def test_negative_news_blocks_entry() -> None:
    strat = OrbIntraday(OrbParams())
    _warmed(strat)
    out = strat.evaluate(
        _ctx(strat, PositionState.WATCHING, 106, 9, 40, news=-0.5, rvol=2.0, atr_14=1.0)
    )
    assert out[0].reason == "news_negative"


def test_intraday_only_resolution_contract() -> None:
    with pytest.raises(ValueError):
        PositionFactory.create(
            Signal(ticker="005930"),
            StrategyTemplate(strategy_id="orb_intraday_v1", resolution=Resolution.D1),
        )
    lot = PositionFactory.create(
        Signal(ticker="005930"),
        StrategyTemplate(strategy_id="orb_intraday_v1", resolution=Resolution.M5),
    )
    assert lot.params.resolution is Resolution.M5


def test_autodiscovered_in_registry() -> None:
    from short_trading_bot.strategy.registry import all_strategies

    assert "orb_intraday_v1" in all_strategies()  # module drop-in, no core edits
