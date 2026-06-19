"""Market-data feed interface + a replay feed for tests / paper-over-history.

The engine consumes ``Feed.stream()`` (an async iterator of Bars), so the same orchestrator
runs over a historical tape (ReplayFeed) or a live KIS WebSocket feed (added in deployment).
Real-time KIS uses 국내 H0STCNT0/H0STASP0/H0STCNI0 and 해외 HDFSCNT0/HDFSASP0/H0GSCNI0 with
a BarBuilder aggregating ticks; that adapter implements this same interface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Protocol, runtime_checkable

from .types import Bar


@runtime_checkable
class Feed(Protocol):
    def stream(self) -> AsyncIterator[Bar]:
        """Yield bars in chronological order."""
        ...


class ReplayFeed:
    """Replays a fixed list of bars (backtest-as-paper / unit tests)."""

    def __init__(self, bars: Iterable[Bar]) -> None:
        self._bars = list(bars)

    async def stream(self) -> AsyncIterator[Bar]:
        for bar in self._bars:
            yield bar
