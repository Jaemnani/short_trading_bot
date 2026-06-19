"""OpenDART disclosure poller (official source).

The HTTP fetch is injected (deployment passes an OpenDartReader/httpx-backed fetch hitting
list.json on a 30-60s cadence); this class owns dedup-by-rcept_no and high-impact flagging.
High-impact disclosure types: B (주요사항: 유상증자/자기주식/합병…) and I (거래소: 조회공시/불성실공시).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .types import NewsArticle

DartFetch = Callable[[], Awaitable[list[dict[str, Any]]]]
HIGH_IMPACT_TYPES = frozenset({"B", "I"})


class DartPoller:
    def __init__(self, fetch: DartFetch) -> None:
        self._fetch = fetch
        self._seen: set[str] = set()

    async def poll(self) -> list[NewsArticle]:
        out: list[NewsArticle] = []
        for row in await self._fetch():
            rcept = str(row.get("rcept_no", "")).strip()
            if not rcept or rcept in self._seen:
                continue
            self._seen.add(rcept)
            stock = str(row.get("stock_code", "")).strip() or None
            out.append(
                NewsArticle(
                    id=rcept,
                    source="DART",
                    title=str(row.get("report_nm", "")),
                    url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                    stock_code=stock,
                    pblntf_ty=row.get("pblntf_ty"),
                    is_disclosure=True,
                )
            )
        return out

    @staticmethod
    def is_high_impact(article: NewsArticle) -> bool:
        return article.pblntf_ty in HIGH_IMPACT_TYPES
