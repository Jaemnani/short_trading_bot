"""거래량 상승 종목 스캐너 — watchlist 후보 발굴.

거래량이 늘고 있는(최근 5일 평균 vs 이전 20일 평균) + 우상향 구조(종가>MA20>MA60)의
종목을 점수화한다. 순수 함수(Bar 리스트 입력)라 데이터 소스와 무관하게 테스트 가능;
CLI(`trader scan`)가 FinanceDataReader로 데이터를 채워 호출한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import Bar

_RECENT = 5  # 최근 거래량 창
_BASE = 20  # 비교 기준 창
_MIN_BARS = 65  # MA60 + 여유


@dataclass(slots=True)
class ScanResult:
    ticker: str
    name: str
    vol_ratio: float  # 최근 5일 평균 거래량 / 이전 20일 평균 (>1 = 증가)
    uptrend: bool  # 종가 > MA20 > MA60
    rvol_today: float  # 당일 거래량 / 20일 평균
    avg_value: float  # 최근 5일 평균 거래대금 (원)
    close: float


def analyze(ticker: str, name: str, bars: list[Bar]) -> ScanResult | None:
    """한 종목의 거래량 추세·우상향 여부 분석. 데이터 부족 시 None."""
    if len(bars) < _MIN_BARS:
        return None
    closes = [float(b.close) for b in bars]
    volumes = [float(b.volume) for b in bars]
    values = [float(b.value) for b in bars]

    recent_vol = sum(volumes[-_RECENT:]) / _RECENT
    base_vol = sum(volumes[-(_RECENT + _BASE) : -_RECENT]) / _BASE
    if base_vol <= 0:
        return None

    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    base20 = sum(volumes[-21:-1]) / 20

    return ScanResult(
        ticker=ticker,
        name=name,
        vol_ratio=recent_vol / base_vol,
        uptrend=closes[-1] > ma20 > ma60,
        rvol_today=(volumes[-1] / base20) if base20 > 0 else 0.0,
        avg_value=sum(values[-_RECENT:]) / _RECENT,
        close=closes[-1],
    )


def scan_volume_leaders(
    candidates: dict[str, tuple[str, list[Bar]]],
    *,
    min_vol_ratio: float = 1.2,  # 거래량이 최소 20% 이상 늘어난 종목만
    min_value: float = 1_000_000_000,  # 최근 평균 거래대금 하한 (유동성, 기본 10억)
    require_uptrend: bool = True,
    top: int = 20,
) -> list[ScanResult]:
    """거래량 상승분 종목을 vol_ratio 내림차순으로 상위 top개 반환."""
    results = []
    for ticker, (name, bars) in candidates.items():
        r = analyze(ticker, name, bars)
        if r is None:
            continue
        if r.vol_ratio < min_vol_ratio or r.avg_value < min_value:
            continue
        if require_uptrend and not r.uptrend:
            continue
        results.append(r)
    results.sort(key=lambda r: r.vol_ratio, reverse=True)
    return results[:top]
