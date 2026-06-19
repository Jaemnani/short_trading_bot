from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from short_trading_bot.domain.enums import Resolution
from short_trading_bot.market.types import IndicatorSnapshot


def make_snapshot(
    close: float,
    *,
    ticker: str = "005930",
    resolution: Resolution = Resolution.D1,
    ts: datetime | None = None,
    bar_count: int = 300,
    **indicators: Any,
) -> IndicatorSnapshot:
    inds: dict[str, float | None] = {
        k: (None if v is None else float(v)) for k, v in indicators.items()
    }
    return IndicatorSnapshot(
        ticker=ticker,
        resolution=resolution,
        ts=ts or datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
        close=Decimal(str(close)),
        bar_count=bar_count,
        indicators=inds,
    )
