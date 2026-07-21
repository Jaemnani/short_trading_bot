"""Technical indicators (numpy) + the per-(ticker, resolution) IndicatorEngine.

Implemented directly (not via pandas-ta) for correct Wilder smoothing, deterministic
results, and no third-party-maintenance risk. Each indicator returns the *latest*
value as ``float`` or ``None`` when there is insufficient warmup. The engine keeps a
bounded rolling window per (ticker, resolution) and computes the snapshot ONCE, fanning
it out to every subscribed lot.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from datetime import date

import numpy as np

from ..domain.enums import Resolution
from .types import Bar, IndicatorSnapshot


def _finite(x: float) -> float | None:
    return x if math.isfinite(x) else None


def _ema_series(values: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    prev = float(values[:n].mean())
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = float(values[i]) * k + prev * (1 - k)
        out[i] = prev
    return out


def sma_last(values: np.ndarray, n: int) -> float | None:
    if len(values) < n:
        return None
    return _finite(float(values[-n:].mean()))


def ema_last(values: np.ndarray, n: int) -> float | None:
    series = _ema_series(values, n)
    last = series[-1]
    return _finite(float(last)) if not np.isnan(last) else None


def rsi_wilder(close: np.ndarray, n: int = 14) -> float | None:
    if len(close) < n + 1:
        return None
    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = float(gains[:n].mean())
    avg_loss = float(losses[:n].mean())
    for i in range(n, len(delta)):
        avg_gain = (avg_gain * (n - 1) + float(gains[i])) / n
        avg_loss = (avg_loss * (n - 1) + float(losses[i])) / n
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return _finite(100.0 - 100.0 / (1.0 + rs))


def macd(
    close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float | None, float | None, float | None]:
    if len(close) < slow + signal:
        return None, None, None
    macd_line = _ema_series(close, fast) - _ema_series(close, slow)
    valid = macd_line[~np.isnan(macd_line)]
    if len(valid) < signal:
        return None, None, None
    signal_series = _ema_series(valid, signal)
    macd_v = float(valid[-1])
    signal_v = float(signal_series[-1])
    return _finite(macd_v), _finite(signal_v), _finite(macd_v - signal_v)


def bollinger(
    close: np.ndarray, n: int = 20, k: float = 2.0
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    if len(close) < n:
        return None, None, None, None, None
    window = close[-n:]
    mid = float(window.mean())
    sd = float(window.std(ddof=0))
    upper = mid + k * sd
    lower = mid - k * sd
    last = float(close[-1])
    pctb = (last - lower) / (upper - lower) if upper != lower else None
    bw = (upper - lower) / mid if mid != 0 else None
    return (
        _finite(mid),
        _finite(upper),
        _finite(lower),
        _finite(pctb) if pctb is not None else None,
        _finite(bw) if bw is not None else None,
    )


def _true_ranges(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    h, lo, pc = high[1:], low[1:], close[:-1]
    result: np.ndarray = np.maximum.reduce([h - lo, np.abs(h - pc), np.abs(lo - pc)])
    return result


def atr_wilder(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> float | None:
    if len(close) < n + 1:
        return None
    trs = _true_ranges(high, low, close)
    atr = float(trs[:n].mean())
    for i in range(n, len(trs)):
        atr = (atr * (n - 1) + float(trs[i])) / n
    return _finite(atr)


def _wilder_smooth(arr: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    if len(arr) < n:
        return out
    s = float(arr[:n].sum())
    out[n - 1] = s
    for i in range(n, len(arr)):
        s = s - s / n + float(arr[i])
        out[i] = s
    return out


def adx_dmi(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14
) -> tuple[float | None, float | None, float | None]:
    if len(close) < n + 1:
        return None, None, None
    up = np.diff(high)
    down = -np.diff(low)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = _true_ranges(high, low, close)

    str_ = _wilder_smooth(tr, n)
    sp = _wilder_smooth(plus_dm, n)
    sm = _wilder_smooth(minus_dm, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * sp / str_
        minus_di = 100.0 * sm / str_
        dx = 100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di)

    pdi = _finite(float(plus_di[-1])) if not np.isnan(plus_di[-1]) else None
    mdi = _finite(float(minus_di[-1])) if not np.isnan(minus_di[-1]) else None

    valid_dx = dx[~np.isnan(dx)]
    if len(valid_dx) < n:
        return None, pdi, mdi
    adx = float(valid_dx[:n].mean())
    for i in range(n, len(valid_dx)):
        adx = (adx * (n - 1) + float(valid_dx[i])) / n
    return _finite(adx), pdi, mdi


def stochastic(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    k: int = 14,
    smooth: int = 3,
    d: int = 3,
) -> tuple[float | None, float | None]:
    if len(close) < k + smooth + d - 2:
        return None, None
    raw = np.full(len(close), np.nan)
    for i in range(k - 1, len(close)):
        hh = float(high[i - k + 1 : i + 1].max())
        ll = float(low[i - k + 1 : i + 1].min())
        raw[i] = 100.0 * (float(close[i]) - ll) / (hh - ll) if hh != ll else 50.0
    raw_valid = raw[~np.isnan(raw)]
    if len(raw_valid) < smooth + d - 1:
        return None, None
    slow_k = np.convolve(raw_valid, np.ones(smooth) / smooth, mode="valid")
    if len(slow_k) < d:
        return _finite(float(slow_k[-1])), None
    slow_d = np.convolve(slow_k, np.ones(d) / d, mode="valid")
    return _finite(float(slow_k[-1])), _finite(float(slow_d[-1]))


def obv(close: np.ndarray, volume: np.ndarray) -> float | None:
    if len(close) < 2:
        return None
    total = 0.0
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            total += float(volume[i])
        elif close[i] < close[i - 1]:
            total -= float(volume[i])
    return _finite(total)


def vwap(typical: np.ndarray, volume: np.ndarray) -> float | None:
    vsum = float(volume.sum())
    if vsum == 0:
        return None
    return _finite(float((typical * volume).sum() / vsum))


def rvol(volume: np.ndarray, n: int = 20) -> float | None:
    if len(volume) < n + 1:
        return None
    mean = float(volume[-(n + 1) : -1].mean())
    if mean == 0:
        return None
    return _finite(float(volume[-1]) / mean)


def highest(values: np.ndarray, n: int) -> float | None:
    if len(values) < n:
        return None
    return _finite(float(values[-n:].max()))


def lowest(values: np.ndarray, n: int) -> float | None:
    if len(values) < n:
        return None
    return _finite(float(values[-n:].min()))


Key = tuple[str, Resolution]


class IndicatorEngine:
    """Per (ticker, resolution): a bounded rolling window for windowed indicators, plus
    running accumulators for cumulative OBV and session-anchored VWAP (so neither is
    corrupted when the window saturates). Computes the snapshot ONCE per completed bar."""

    def __init__(self, window: int = 300) -> None:
        self._window = window
        self._bars: dict[Key, deque[Bar]] = {}
        self._obv: dict[Key, float] = {}
        self._last_close: dict[Key, float] = {}
        self._vwap: dict[Key, tuple[date, float, float]] = {}  # (session_date, Σpv, Σv)

    def update(self, bar: Bar) -> IndicatorSnapshot:
        key: Key = (bar.ticker, bar.resolution)
        dq = self._bars.get(key)
        if dq is None:
            dq = deque(maxlen=self._window)
            self._bars[key] = dq
        dq.append(bar)
        self._update_obv(key, bar)
        self._update_vwap(key, bar)
        return self._snapshot(bar, list(dq), key)

    def _update_obv(self, key: Key, bar: Bar) -> None:
        close = float(bar.close)
        prev = self._last_close.get(key)
        total = self._obv.get(key, 0.0)
        if prev is not None:
            if close > prev:
                total += float(bar.volume)
            elif close < prev:
                total -= float(bar.volume)
        self._obv[key] = total
        self._last_close[key] = close

    def _update_vwap(self, key: Key, bar: Bar) -> None:
        day = bar.ts.date()
        typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0
        vol = float(bar.volume)
        state = self._vwap.get(key)
        if state is None or state[0] != day:  # new session resets intraday VWAP
            sum_pv, sum_v = 0.0, 0.0
        else:
            sum_pv, sum_v = state[1], state[2]
        self._vwap[key] = (day, sum_pv + typical * vol, sum_v + vol)

    def _snapshot(self, last: Bar, bars: Sequence[Bar], key: Key) -> IndicatorSnapshot:
        close = np.array([float(b.close) for b in bars])
        high = np.array([float(b.high) for b in bars])
        low = np.array([float(b.low) for b in bars])
        volume = np.array([float(b.volume) for b in bars])

        macd_v, macd_signal, macd_hist = macd(close)
        bb_mid, bb_upper, bb_lower, bb_pctb, bb_bw = bollinger(close)
        adx, plus_di, minus_di = adx_dmi(high, low, close)
        stoch_k, stoch_d = stochastic(high, low, close)

        vwap_state = self._vwap.get(key)
        vwap_val = (
            _finite(vwap_state[1] / vwap_state[2])
            if vwap_state is not None and vwap_state[2] > 0
            else None
        )

        indicators: dict[str, float | None] = {
            "sma_5": sma_last(close, 5),
            "sma_20": sma_last(close, 20),
            "sma_60": sma_last(close, 60),
            "sma_120": sma_last(close, 120),
            "ema_12": ema_last(close, 12),
            "ema_26": ema_last(close, 26),
            "rsi_14": rsi_wilder(close, 14),
            "macd": macd_v,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "bb_mid": bb_mid,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "bb_pctb": bb_pctb,
            "bb_bw": bb_bw,
            "atr_14": atr_wilder(high, low, close, 14),
            "adx_14": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "stoch_k": stoch_k,
            "stoch_d": stoch_d,
            "obv": _finite(self._obv.get(key, 0.0)),
            "vwap": vwap_val,
            "rvol": rvol(volume, 20),
            "high_20": highest(high, 20),
            "low_20": lowest(low, 20),
        }
        return IndicatorSnapshot(
            ticker=last.ticker,
            resolution=last.resolution,
            ts=last.ts,
            close=last.close,
            bar_count=len(bars),
            indicators=indicators,
            high=last.high,
            low=last.low,
            volume=last.volume,
        )
