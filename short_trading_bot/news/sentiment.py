"""Sentiment scoring: Scorer protocol + a license-safe Korean keyword LexiconScorer.

The lexicon scorer is the default because it is transparent, low-latency, and carries no
model-license risk (the KR-FinBert-SC license is unclear for commercial use). KR-FinBert /
Claude-API scorers can be dropped in behind the same ``Scorer`` protocol.
Score is in [-1, 1]; |score| < dead_band collapses to neutral 0.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

_POSITIVE = (
    "상한가", "어닝서프라이즈", "흑자전환", "자기주식취득", "자사주", "신고가",
    "수주", "공급계약", "호실적", "최대실적", "흑자", "수혜",
)
_NEGATIVE = (
    "하한가", "어닝쇼크", "적자전환", "유상증자", "감자", "불성실공시", "소송",
    "영업정지", "횡령", "배임", "상장폐지", "거래정지", "적자", "리콜",
)


@runtime_checkable
class Scorer(Protocol):
    def score(self, text: str) -> float:
        """Return sentiment in [-1, 1] (positive = bullish)."""
        ...


class LexiconScorer:
    def __init__(self, *, dead_band: float = 0.2, step: float = 0.5) -> None:
        self._dead_band = dead_band
        self._step = step

    def score(self, text: str) -> float:
        pos = sum(1 for k in _POSITIVE if k in text)
        neg = sum(1 for k in _NEGATIVE if k in text)
        raw = max(-1.0, min(1.0, (pos - neg) * self._step))
        return 0.0 if abs(raw) < self._dead_band else raw
