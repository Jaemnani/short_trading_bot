"""Press-RSS poller (official, ToS-friendly: 한국경제/매일경제/파이낸셜뉴스 등).

The fetch is injected (deployment uses feedparser/httpx). Dedups by a hash of (url|title)
so the same wire story isn't re-scored. Only metadata is kept — never article bodies.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from .types import NewsArticle

RssFetch = Callable[[], Awaitable[list[dict[str, Any]]]]


class RssPoller:
    def __init__(self, fetch: RssFetch, *, outlet: str = "rss") -> None:
        self._fetch = fetch
        self._outlet = outlet
        self._seen: set[str] = set()

    async def poll(self) -> list[NewsArticle]:
        out: list[NewsArticle] = []
        for entry in await self._fetch():
            url = str(entry.get("link", ""))
            title = str(entry.get("title", ""))
            key = hashlib.sha1(f"{url}|{title}".encode()).hexdigest()
            if key in self._seen:
                continue
            self._seen.add(key)
            out.append(
                NewsArticle(id=key, source=f"RSS:{self._outlet}", title=title, url=url)
            )
        return out
