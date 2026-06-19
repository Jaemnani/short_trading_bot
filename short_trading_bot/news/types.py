"""News/disclosure value objects. Only metadata is stored (titles/URLs/scores) — never
full article bodies (copyright/ToS), per the commercial-use decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class NewsArticle:
    id: str  # DART rcept_no, or a hash of (url|title) for RSS
    source: str  # "DART" | "RSS:<outlet>"
    title: str
    url: str
    published_at: datetime | None = None
    stock_code: str | None = None  # authoritative ticker (DART carries it)
    pblntf_ty: str | None = None  # DART disclosure type (B=주요사항, I=거래소, ...)
    is_disclosure: bool = False
