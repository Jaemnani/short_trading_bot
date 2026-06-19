"""Aggregate ticks into fixed minute bars (1/3/5/10/15/30/60m) per ticker.

One BarBuilder per intraday resolution; it buckets each ticker's ticks by
``floor(epoch / bar_seconds)``. Ticks are assumed time-ordered per ticker.
Daily/weekly/monthly bars come from period-bar loaders, not from tick aggregation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..domain.enums import Resolution
from .types import Bar, Tick


class _OpenBar:
    __slots__ = ("bucket", "close", "high", "low", "open", "value", "volume")

    def __init__(self, bucket: int, tick: Tick) -> None:
        self.bucket = bucket
        self.open = tick.price
        self.high = tick.price
        self.low = tick.price
        self.close = tick.price
        self.volume = tick.volume
        self.value = tick.price * tick.volume

    def update(self, tick: Tick) -> None:
        if tick.price > self.high:
            self.high = tick.price
        if tick.price < self.low:
            self.low = tick.price
        self.close = tick.price
        self.volume += tick.volume
        self.value += tick.price * tick.volume

    def finish(self, resolution: Resolution, ticker: str) -> Bar:
        return Bar(
            ticker=ticker,
            resolution=resolution,
            ts=datetime.fromtimestamp(self.bucket, tz=UTC),
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            value=self.value,
        )


class BarBuilder:
    def __init__(self, resolution: Resolution) -> None:
        seconds = resolution.bar_seconds
        if seconds is None:
            raise ValueError(f"{resolution} is not an aggregatable minute resolution")
        self.resolution = resolution
        self._seconds = seconds
        self._open: dict[str, _OpenBar] = {}

    def on_tick(self, tick: Tick) -> list[Bar]:
        """Feed a tick; return any bar(s) completed by it (0 or 1)."""
        bucket = int(tick.ts.timestamp() // self._seconds) * self._seconds
        ob = self._open.get(tick.ticker)
        if ob is None:
            self._open[tick.ticker] = _OpenBar(bucket, tick)
            return []
        if bucket == ob.bucket:
            ob.update(tick)
            return []
        if bucket < ob.bucket:
            raise ValueError(f"out-of-order tick for {tick.ticker}: {tick.ts}")
        completed = ob.finish(self.resolution, tick.ticker)
        self._open[tick.ticker] = _OpenBar(bucket, tick)
        return [completed]

    def flush(self, ticker: str | None = None) -> list[Bar]:
        """Close and emit currently-open bar(s) (e.g. at session end)."""
        tickers = [ticker] if ticker is not None else list(self._open)
        out: list[Bar] = []
        for t in tickers:
            ob = self._open.pop(t, None)
            if ob is not None:
                out.append(ob.finish(self.resolution, t))
        return out
