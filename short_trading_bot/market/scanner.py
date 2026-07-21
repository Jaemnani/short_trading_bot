"""거래량 상승 종목 스캐너 — watchlist 후보 발굴 + 장중 모멘텀 픽.

일봉 파트: 거래량이 늘고 있는(최근 5일 평균 vs 이전 20일 평균) + 우상향 구조
(종가>MA20>MA60)의 종목을 점수화한다. 순수 함수(Bar 리스트 입력)라 데이터 소스와
무관하게 테스트 가능; CLI(`trader scan`)가 FinanceDataReader로 데이터를 채워 호출한다.

장중 파트(``pick_momentum``): KIS 거래량순위(RankRow)에서 급등 + 거래량 급증 +
유동성 조건을 통과한 종목을 고른다 — 실시간 스캐너의 필터 코어(순수 함수).
며칠짜리 일봉 스캔을 통과한 '후보군(favorites)'은 완화된 문턱을 적용해 우선 합류.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from .kis_ranking import RankRow
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


@dataclass(slots=True)
class UniversePick:
    """눌림목 모델 적합 종목 — 25종목 검증에서 확인된 판별 기준으로 선별."""

    ticker: str
    name: str
    close: float
    atr_pct: float  # ATR(14)/종가 — 변동성 (0.05 초과 = 수직 급등주, 부적합)
    avg_value: float  # 최근 20일 평균 거래대금
    ret_6m: float  # 최근 ~6개월(120봉) 수익률 — 랭킹 기준


def select_pullback_universe(
    candidates: dict[str, tuple[str, list[Bar]]],
    *,
    min_value: float = 5_000_000_000,  # 유동성 하한 (20일 평균 거래대금)
    max_atr_pct: float = 0.05,  # 변동성 상한 (두산에너빌 -27% 사례 차단)
    top: int = 5,
) -> list[UniversePick]:
    """눌림목(pullback_daily_v1)에 맞는 종목 선별 — 진짜 장기 상승추세만.

    기준(25종목 검증 근거): 종가 > MA120(장기 추세) + 종가 > MA60, MA20 > MA60
    (정배열) + ATR% ≤ 상한 + 유동성. 랭킹은 6개월 수익률(추세 강도) 내림차순.
    """
    picks: list[UniversePick] = []
    for ticker, (name, bars) in candidates.items():
        if len(bars) < 130:
            continue
        closes = [float(b.close) for b in bars]
        close = closes[-1]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        ma120 = sum(closes[-120:]) / 120
        if not (close > ma60 and ma20 > ma60 and close > ma120):
            continue
        trs = [
            max(float(b.high) - float(b.low),
                abs(float(b.high) - closes[i - 1]),
                abs(float(b.low) - closes[i - 1]))
            for i, b in enumerate(bars[-15:], start=len(bars) - 15)
        ]
        atr_pct = (sum(trs) / len(trs)) / close if close > 0 else 1.0
        if atr_pct > max_atr_pct:
            continue
        avg_value = sum(float(b.value) for b in bars[-20:]) / 20
        if avg_value < min_value:
            continue
        picks.append(UniversePick(
            ticker=ticker, name=name, close=close, atr_pct=round(atr_pct, 4),
            avg_value=avg_value, ret_6m=close / closes[-120] - 1.0,
        ))
    picks.sort(key=lambda p: p.ret_6m, reverse=True)
    return picks[:top]


@dataclass(slots=True)
class MomentumPick:
    ticker: str
    name: str
    change_pct: float
    vol_surge: float
    value_traded: float
    favorite: bool  # 일봉 후보군 출신 (완화 문턱으로 통과)


def pick_momentum(
    rows: Iterable[RankRow],
    *,
    min_change_pct: float = 3.0,  # 등락률 하한 (%)
    max_change_pct: float | None = 15.0,  # 등락률 상한 (%) — 과열 추격 방지, None=무제한
    min_vol_surge: float = 150.0,  # 거래량증가율 하한 (%, 전일 대비)
    min_value: float = 5_000_000_000,  # 누적 거래대금 하한 (원)
    exclude: Collection[str] = (),
    favorites: Collection[str] = (),
    favorite_relax: float = 0.7,  # 후보군은 문턱 x0.7
    top: int = 3,
) -> list[MomentumPick]:
    """상승세 + 거래량 급증(인기) 종목을 고른다. favorites 우선, 이후 거래량증가율순.

    ``max_change_pct``: 이미 너무 오른 종목(상한가 추격 등)은 되돌림 리스크가 커서
    걸러낸다 — 7/13주 시뮬레이션에서 +25~30% 합류 건들이 전부 손실이었다."""
    picks: list[MomentumPick] = []
    for row in rows:
        if not row.ticker or row.ticker in exclude:
            continue
        fav = row.ticker in favorites
        relax = favorite_relax if fav else 1.0
        if row.change_pct < min_change_pct * relax:
            continue
        if max_change_pct is not None and row.change_pct > max_change_pct:
            continue
        if row.vol_surge < min_vol_surge * relax:
            continue
        if row.value_traded < min_value * relax:
            continue
        picks.append(
            MomentumPick(
                ticker=row.ticker,
                name=row.name,
                change_pct=row.change_pct,
                vol_surge=row.vol_surge,
                value_traded=row.value_traded,
                favorite=fav,
            )
        )
    picks.sort(key=lambda p: (not p.favorite, -p.vol_surge))
    return picks[:top]
