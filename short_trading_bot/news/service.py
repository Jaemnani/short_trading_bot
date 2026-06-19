"""NewsService — ties pollers → ticker mapping → sentiment → per-ticker EWMA.

The engine reads ``ewma(ticker)`` as the news gate/size signal, and ``halted(ticker)`` as a
hard no-auto-buy circuit breaker. Run :meth:`poll_once` on a scheduler (DART 30-60s,
RSS 60-180s).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from .aggregator import SentimentAggregator
from .mapper import TickerMapper
from .sentiment import LexiconScorer, Scorer
from .types import NewsArticle


class Poller(Protocol):
    async def poll(self) -> list[NewsArticle]: ...


class NewsService:
    def __init__(
        self,
        pollers: Sequence[Poller],
        mapper: TickerMapper,
        *,
        scorer: Scorer | None = None,
        aggregator: SentimentAggregator | None = None,
    ) -> None:
        self._pollers = list(pollers)
        self._mapper = mapper
        self._scorer = scorer or LexiconScorer()
        self._agg = aggregator or SentimentAggregator()

    async def poll_once(self, now: datetime) -> int:
        ingested = 0
        for poller in self._pollers:
            for article in await poller.poll():
                ticker = self._mapper.map(article)
                if ticker is None:
                    continue
                self._agg.ingest(ticker, self._scorer.score(article.title), now, title=article.title)
                ingested += 1
        return ingested

    def ewma(self, ticker: str, now: datetime | None = None) -> float | None:
        return self._agg.ewma(ticker, now)

    def halted(self, ticker: str) -> bool:
        return self._agg.halted(ticker)
