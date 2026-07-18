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


# -- 장중 모멘텀 픽 (pick_momentum) ------------------------------------------


def _rank(ticker: str, *, change: float = 5.0, surge: float = 300.0, value: float = 2e10):
    from short_trading_bot.market.kis_ranking import RankRow

    return RankRow(
        ticker=ticker, name=ticker, price=10000.0, change_pct=change,
        volume=1e6, value_traded=value, vol_surge=surge,
    )


def test_pick_momentum_filters_and_sorts() -> None:
    from short_trading_bot.market.scanner import pick_momentum

    rows = [
        _rank("A", surge=500.0),
        _rank("B", surge=900.0),
        _rank("C", change=1.0),  # 등락률 미달
        _rank("D", surge=100.0),  # 거래량증가율 미달
        _rank("E", value=1e9),  # 거래대금 미달
    ]
    picks = pick_momentum(rows)
    assert [p.ticker for p in picks] == ["B", "A"]  # 급증률 내림차순


def test_pick_momentum_excludes_active_and_relaxes_favorites() -> None:
    from short_trading_bot.market.scanner import pick_momentum

    rows = [_rank("A", surge=500.0), _rank("F", change=2.5, surge=120.0, value=4e9)]
    # F는 일반 문턱(3%/150%/50억)엔 못 미치지만 후보군 완화(x0.7)로 통과 + 우선 정렬
    picks = pick_momentum(rows, favorites={"F"})
    assert [p.ticker for p in picks] == ["F", "A"] and picks[0].favorite

    assert [p.ticker for p in pick_momentum(rows, exclude={"A"}, favorites={"F"})] == ["F"]
