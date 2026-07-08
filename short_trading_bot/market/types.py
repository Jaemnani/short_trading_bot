"""Market data value objects: Tick, Bar (OHLCV), and the IndicatorSnapshot fanned
out to every PositionLot subscribed to a (ticker, resolution).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..domain.enums import Resolution


@dataclass(slots=True)
class Tick:
    ticker: str
    price: Decimal
    volume: Decimal  # traded quantity of this print
    ts: datetime


@dataclass(slots=True)
class Bar:
    ticker: str
    resolution: Resolution
    ts: datetime  # bar OPEN time (start of the bucket), tz-aware
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal = Decimal(0)  # 거래대금 (Σ price*qty) — preferred over share count for KR


@dataclass(slots=True)
class IndicatorSnapshot:
    """Computed once per (ticker, resolution) and shared with all subscribed lots.

    ``indicators`` values are ``None`` until enough warmup bars exist.
    """

    ticker: str
    resolution: Resolution
    ts: datetime
    close: Decimal
    bar_count: int
    indicators: dict[str, float | None] = field(default_factory=dict)
    high: Decimal = Decimal(0)  # last bar's high/low/volume (intraday strategies need them)
    low: Decimal = Decimal(0)
    volume: Decimal = Decimal(0)

    def get(self, key: str) -> float | None:
        return self.indicators.get(key)
