"""거래량 상승 종목 스캐너 tests (offline, synthetic bars)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from short_trading_bot.domain.enums import Resolution
from short_trading_bot.market.scanner import analyze, scan_volume_leaders
from short_trading_bot.market.types import Bar

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _bars(closes: list[float], volumes: list[float], ticker: str = "T") -> list[Bar]:
    out = []
    for i, (c, v) in enumerate(zip(closes, volumes, strict=True)):
        cd, vd = Decimal(str(c)), Decimal(str(v))
        out.append(
            Bar(ticker=ticker, resolution=Resolution.D1, ts=BASE + timedelta(days=i),
                open=cd, high=cd, low=cd, close=cd, volume=vd, value=cd * vd)
        )
    return out


def _uptrend_rising_vol() -> list[Bar]:
    closes = [100_000 + i * 500 for i in range(70)]  # steady uptrend (실제 주가 스케일)
    volumes = [100_000.0] * 65 + [250_000.0] * 5  # recent 5d volume 2.5x → 거래대금 ~10^10
    return _bars(closes, volumes)


def test_analyze_flags_rising_volume_uptrend() -> None:
    r = analyze("005930", "삼성전자", _uptrend_rising_vol())
    assert r is not None
    assert r.uptrend is True
    assert r.vol_ratio > 2.0


def test_analyze_insufficient_data() -> None:
    assert analyze("X", "짧음", _bars([100] * 30, [1000] * 30)) is None


def test_scan_filters_and_ranks() -> None:
    rising = _uptrend_rising_vol()
    falling_vol = _bars([100_000 + i * 500 for i in range(70)], [200_000.0] * 65 + [80_000.0] * 5)
    downtrend = _bars([170_000 - i * 1000 for i in range(70)], [100_000.0] * 65 + [300_000.0] * 5)
    tiny_value = _bars([100 + i for i in range(70)], [100.0] * 65 + [300.0] * 5)

    leaders = scan_volume_leaders(
        {
            "A": ("거래량증가+우상향", rising),
            "B": ("거래량감소", falling_vol),
            "C": ("거래량증가+하락", downtrend),
            "D": ("거래대금부족", tiny_value),
        }
    )
    assert [r.ticker for r in leaders] == ["A"]  # 거래량 감소·하락추세·저유동성 모두 제외


def test_scan_allows_non_uptrend_when_disabled() -> None:
    downtrend = _bars([170_000 - i * 1000 for i in range(70)], [100_000.0] * 65 + [300_000.0] * 5)
    leaders = scan_volume_leaders(
        {"C": ("하락추세", downtrend)}, require_uptrend=False, min_value=0
    )
    assert len(leaders) == 1 and leaders[0].uptrend is False
