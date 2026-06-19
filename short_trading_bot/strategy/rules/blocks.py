"""Reusable signal building blocks operating on IndicatorSnapshot(s).

Algorithms compose these; each is None-safe (missing warmup -> the gate fails / the
exit reason is skipped). Cross/breakout checks need the previous snapshot.
"""

from __future__ import annotations

from ...market.types import IndicatorSnapshot


def _all(*values: float | None) -> bool:
    return all(v is not None for v in values)


def regime_bullish(
    snap: IndicatorSnapshot, *, adx_min: float, require_full_alignment: bool = False
) -> bool:
    """Trend gate: MA alignment + ADX>=min + +DI>-DI."""
    close = float(snap.close)
    sma20, sma60 = snap.get("sma_20"), snap.get("sma_60")
    adx, plus_di, minus_di = snap.get("adx_14"), snap.get("plus_di"), snap.get("minus_di")
    if not _all(sma20, sma60, adx, plus_di, minus_di):
        return False
    assert sma20 is not None and sma60 is not None
    assert adx is not None and plus_di is not None and minus_di is not None

    aligned = close > sma20 > sma60
    if require_full_alignment:
        sma5 = snap.get("sma_5")
        if sma5 is None:
            return False
        aligned = aligned and close > sma5 > sma20
    return aligned and adx >= adx_min and plus_di > minus_di


def macd_cross_up(snap: IndicatorSnapshot, prev: IndicatorSnapshot | None) -> bool:
    if prev is None:
        return False
    m0, s0 = prev.get("macd"), prev.get("macd_signal")
    m1, s1 = snap.get("macd"), snap.get("macd_signal")
    if not _all(m0, s0, m1, s1):
        return False
    assert m0 is not None and s0 is not None and m1 is not None and s1 is not None
    return m0 <= s0 and m1 > s1


def golden_cross(snap: IndicatorSnapshot, prev: IndicatorSnapshot | None) -> bool:
    if prev is None:
        return False
    f0, sl0 = prev.get("sma_5"), prev.get("sma_20")
    f1, sl1 = snap.get("sma_5"), snap.get("sma_20")
    if not _all(f0, sl0, f1, sl1):
        return False
    assert f0 is not None and sl0 is not None and f1 is not None and sl1 is not None
    return f0 <= sl0 and f1 > sl1


def breakout_high(snap: IndicatorSnapshot, prev: IndicatorSnapshot | None) -> bool:
    """Close breaks above the prior 20-bar high (no look-ahead: uses prev's high_20)."""
    if prev is None:
        return False
    hi = prev.get("high_20")
    if hi is None:
        return False
    return float(snap.close) > hi


def momentum_ok(snap: IndicatorSnapshot, *, rsi_low: float, rsi_high: float) -> bool:
    rsi, hist = snap.get("rsi_14"), snap.get("macd_hist")
    if not _all(rsi, hist):
        return False
    assert rsi is not None and hist is not None
    return rsi_low <= rsi <= rsi_high and hist > 0


def volume_ok(snap: IndicatorSnapshot, *, rvol_min: float) -> bool:
    rvol = snap.get("rvol")
    return rvol is not None and rvol >= rvol_min


def exit_reasons(snap: IndicatorSnapshot, prev: IndicatorSnapshot | None) -> list[str]:
    """Indicator-based exit signals (risk/stop exits are handled by the strategy)."""
    reasons: list[str] = []
    close = float(snap.close)

    sma20 = snap.get("sma_20")
    if sma20 is not None and close < sma20:
        reasons.append("below_ma20")

    if prev is not None:
        f0, sl0 = prev.get("sma_5"), prev.get("sma_20")
        f1, sl1 = snap.get("sma_5"), snap.get("sma_20")
        if _all(f0, sl0, f1, sl1) and f0 >= sl0 and f1 < sl1:  # type: ignore[operator]
            reasons.append("dead_cross_5_20")

        m0, sg0 = prev.get("macd"), prev.get("macd_signal")
        m1, sg1, h1 = snap.get("macd"), snap.get("macd_signal"), snap.get("macd_hist")
        if _all(m0, sg0, m1, sg1, h1) and m0 >= sg0 and m1 < sg1 and h1 < 0:  # type: ignore[operator]
            reasons.append("macd_dead_cross")

        r0, r1 = prev.get("rsi_14"), snap.get("rsi_14")
        if _all(r0, r1) and r0 > 70 and r1 < r0:  # type: ignore[operator]
            reasons.append("rsi_rolldown")

        a0, a1 = prev.get("adx_14"), snap.get("adx_14")
        pdi, mdi = snap.get("plus_di"), snap.get("minus_di")
        if _all(a0, a1, pdi, mdi) and a1 < a0 and mdi > pdi:  # type: ignore[operator]
            reasons.append("adx_weakening")

    return reasons
