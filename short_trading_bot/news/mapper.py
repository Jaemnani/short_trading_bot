"""Map a news article to a 종목코드.

DART carries an authoritative ``stock_code``; for free-text RSS, fall back to a
name->code dictionary (lower confidence). Build the dictionary from a listing
(FinanceDataReader/pykrx) in deployment.
"""

from __future__ import annotations

from .types import NewsArticle


class TickerMapper:
    def __init__(self, name_to_code: dict[str, str] | None = None) -> None:
        # Longer names first so e.g. "삼성전자우" matches before "삼성전자".
        self._names = sorted((name_to_code or {}).items(), key=lambda kv: -len(kv[0]))

    def map(self, article: NewsArticle) -> str | None:
        if article.stock_code:
            return article.stock_code  # authoritative
        for name, code in self._names:
            if name in article.title:
                return code  # low-confidence free-text match
        return None
