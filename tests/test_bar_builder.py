from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from short_trading_bot.domain.enums import Resolution
from short_trading_bot.market.bar_builder import BarBuilder
from short_trading_bot.market.types import Tick

BASE = datetime(2026, 1, 2, 9, 0, 0, tzinfo=UTC)  # minute-aligned


def _tick(off: int, price: str, vol: str) -> Tick:
    return Tick("005930", Decimal(price), Decimal(vol), BASE + timedelta(seconds=off))


def test_1m_aggregation_and_value() -> None:
    bb = BarBuilder(Resolution.M1)
    assert bb.on_tick(_tick(0, "100", "10")) == []
    assert bb.on_tick(_tick(30, "105", "5")) == []
    assert bb.on_tick(_tick(50, "95", "5")) == []
    completed = bb.on_tick(_tick(70, "101", "1"))  # next minute -> close minute 0

    assert len(completed) == 1
    bar = completed[0]
    assert (bar.open, bar.high, bar.low, bar.close) == (
        Decimal("100"),
        Decimal("105"),
        Decimal("95"),
        Decimal("95"),
    )
    assert bar.volume == Decimal("20")
    assert bar.value == Decimal("2000")  # 100*10 + 105*5 + 95*5
    assert bar.ts == BASE


def test_flush_emits_open_bar() -> None:
    bb = BarBuilder(Resolution.M1)
    bb.on_tick(_tick(0, "100", "10"))
    out = bb.flush()
    assert len(out) == 1 and out[0].close == Decimal("100")
    assert bb.flush() == []  # nothing left


def test_non_aggregatable_resolution_rejected() -> None:
    with pytest.raises(ValueError):
        BarBuilder(Resolution.D1)


def test_out_of_order_tick_rejected() -> None:
    bb = BarBuilder(Resolution.M1)
    bb.on_tick(_tick(70, "100", "1"))  # minute 1
    with pytest.raises(ValueError):
        bb.on_tick(_tick(0, "100", "1"))  # minute 0 < current


def test_multiple_tickers_independent() -> None:
    bb = BarBuilder(Resolution.M5)  # 300s buckets
    bb.on_tick(Tick("A", Decimal("10"), Decimal("1"), BASE))
    bb.on_tick(Tick("B", Decimal("20"), Decimal("1"), BASE))
    a = bb.on_tick(Tick("A", Decimal("11"), Decimal("1"), BASE + timedelta(seconds=300)))
    assert len(a) == 1 and a[0].ticker == "A" and a[0].open == Decimal("10")
