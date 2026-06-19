"""Per-ticker sentiment EWMA + hard event overrides.

Each article nudges a per-ticker EWMA that decays toward neutral (0) with a half-life, so
stale news fades. Disclosures containing circuit-breaker keywords (상장폐지/거래정지/횡령…)
mark the ticker halted and force the EWMA strongly negative — the strategy must never
auto-buy these.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

_HALT_KEYWORDS = ("상장폐지", "거래정지", "횡령", "배임", "감사의견거절", "관리종목")


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


@dataclass(slots=True)
class _State:
    ewma: float
    last_ts: datetime
    halted: bool = False


class SentimentAggregator:
    def __init__(self, *, half_life_hours: float = 4.0, alpha: float = 0.5) -> None:
        self._hl = half_life_hours
        self._alpha = alpha
        self._state: dict[str, _State] = {}

    def _decay(self, ewma: float, frm: datetime, to: datetime) -> float:
        elapsed_h = (to - frm).total_seconds() / 3600.0
        if elapsed_h <= 0:
            return ewma
        return float(ewma * (0.5 ** (elapsed_h / self._hl)))

    def ingest(self, ticker: str, score: float, ts: datetime, *, title: str = "") -> None:
        state = self._state.get(ticker)
        if state is None:
            state = _State(ewma=0.0, last_ts=ts)
            self._state[ticker] = state
        else:
            state.ewma = self._decay(state.ewma, state.last_ts, ts)
            state.last_ts = ts
        state.ewma = _clamp(state.ewma * (1 - self._alpha) + score * self._alpha)
        if any(k in title for k in _HALT_KEYWORDS):
            state.halted = True
            state.ewma = -1.0

    def ewma(self, ticker: str, now: datetime | None = None) -> float | None:
        state = self._state.get(ticker)
        if state is None:
            return None
        if now is None:
            return state.ewma
        return _clamp(self._decay(state.ewma, state.last_ts, now))

    def halted(self, ticker: str) -> bool:
        state = self._state.get(ticker)
        return state.halted if state is not None else False
