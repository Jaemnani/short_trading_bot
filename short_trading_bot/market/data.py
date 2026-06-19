"""Historical/period bar sources.

``BarSource`` is the loader contract. ``InMemoryBarSource`` backs unit tests and the
backtest harness (replay a tape). A FinanceDataReader/pykrx adapter implements the
same protocol for real KR daily data (added when wiring live data; both libs have
documented version breakage, so they are an optional extra, not a core dependency).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..domain.enums import Resolution
from .types import Bar


@runtime_checkable
class BarSource(Protocol):
    def load(
        self,
        ticker: str,
        resolution: Resolution,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        """Return bars for (ticker, resolution) within [start, end], ascending by ts."""
        ...


class InMemoryBarSource:
    """Holds pre-loaded bars; used for tests and backtest tapes."""

    def __init__(self, bars: Iterable[Bar] = ()) -> None:
        self._bars: dict[tuple[str, Resolution], list[Bar]] = defaultdict(list)
        for bar in bars:
            self._bars[(bar.ticker, bar.resolution)].append(bar)
        for series in self._bars.values():
            series.sort(key=lambda b: b.ts)

    def add(self, bar: Bar) -> None:
        series = self._bars[(bar.ticker, bar.resolution)]
        series.append(bar)
        series.sort(key=lambda b: b.ts)

    def load(
        self,
        ticker: str,
        resolution: Resolution,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        series = self._bars.get((ticker, resolution), [])
        return [
            b
            for b in series
            if (start is None or b.ts >= start) and (end is None or b.ts <= end)
        ]

    def tape(self) -> list[Bar]:
        """All bars across tickers/resolutions, globally ordered by ts (replay order)."""
        flat = [b for series in self._bars.values() for b in series]
        return sorted(flat, key=lambda b: b.ts)
