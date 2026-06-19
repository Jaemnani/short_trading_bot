from datetime import UTC, datetime, timedelta
from decimal import Decimal

from short_trading_bot.domain.enums import Resolution
from short_trading_bot.market.data import InMemoryBarSource
from short_trading_bot.market.indicators import IndicatorEngine
from short_trading_bot.market.types import Bar

BASE = datetime(2026, 1, 2, 9, 0, 0, tzinfo=UTC)


def _bar(i: int, close: float, resolution: Resolution = Resolution.M1, ticker: str = "005930") -> Bar:
    c = Decimal(str(close))
    return Bar(
        ticker=ticker,
        resolution=resolution,
        ts=BASE + timedelta(minutes=i),
        open=c,
        high=c + Decimal("1"),
        low=c - Decimal("1"),
        close=c,
        volume=Decimal("100"),
        value=c * Decimal("100"),
    )


def test_engine_warms_up_then_computes() -> None:
    engine = IndicatorEngine()
    snap = None
    for i in range(40):
        snap = engine.update(_bar(i, 100 + i))  # steady uptrend
    assert snap is not None
    assert snap.bar_count == 40
    assert snap.ticker == "005930" and snap.resolution == Resolution.M1
    assert snap.get("sma_5") is not None
    assert snap.get("rsi_14") == 100.0  # monotonic up
    assert snap.get("adx_14") is not None
    assert snap.get("macd") is not None


def test_engine_insufficient_warmup_is_none() -> None:
    engine = IndicatorEngine()
    snap = engine.update(_bar(0, 100))
    assert snap.bar_count == 1
    assert snap.get("sma_20") is None
    assert snap.get("rsi_14") is None


def test_engine_keys_resolutions_independently() -> None:
    engine = IndicatorEngine()
    engine.update(_bar(0, 100, Resolution.M1))
    engine.update(_bar(1, 101, Resolution.M1))
    snap_m5 = engine.update(_bar(0, 200, Resolution.M5))
    assert snap_m5.bar_count == 1  # separate window from M1
    assert snap_m5.resolution == Resolution.M5


def test_inmemory_bar_source_tape_order() -> None:
    src = InMemoryBarSource([_bar(2, 102), _bar(0, 100), _bar(1, 101)])
    tape = src.tape()
    assert [b.ts for b in tape] == sorted(b.ts for b in tape)
    loaded = src.load("005930", Resolution.M1)
    assert len(loaded) == 3
