"""팩터(저PBR+흑자) 종목 선별 — DART 재무 조회 + 순수 선별 함수.

10년 검증(2016~2026, 가설 사전등록 → 채택)된 팩터 전략의 실행 도구:
"시총 상위 200 중 저PBR + 흑자 상위 10종목 동일가중, 분기 리밸런스"
— +353%(코스피 +330%), 최악 해 -4%, 2022 하락장 0%, 눌림목과 월상관 -0.01.
12개월 롤링 71% 플러스/중앙값 +10.3% (코스피 59%/+4.4%).

검증과 동일한 규칙을 그대로 따른다:
- PBR = 시가총액 / 자본총계 (연결 CFS 우선, 없으면 별도 OFS 폴백)
- 흑자 = 당기순이익 > 0 (계정명 '당기순이익' **부분일치** — "당기순이익(손실)"
  표기 회사가 있어 정확일치는 다수 누락된다, 검증 중 실측)
- 선견 차단: Y년 사업보고서 재무는 Y+1년 7월부터만 사용 (제출 지연 여유)

DART 조회는 transport 주입으로 오프라인 테스트 가능 (리포 표준 패턴).
corp_code 매핑은 DartRiskChecker와 같은 캐시(data/dart_corp_codes.json)를 공유한다.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..infra.logging import get_logger

# (url, params) -> 응답 바이트
Transport = Callable[[str, dict[str, str]], Awaitable[bytes]]

_BASE = "https://opendart.fss.or.kr/api"
_ANNUAL_REPORT = "11011"  # 사업보고서


def applicable_fiscal_year(today: date) -> int:
    """지금 시점에 써도 되는 사업연도 — Y년 재무는 Y+1년 7월부터 (선견 차단)."""
    return today.year - 1 if today.month >= 7 else today.year - 2


@dataclass(slots=True)
class FundamentalRow:
    """한 종목의 선별 입력 — 시세(시총·종가)는 호출자가, 재무는 DART가 채운다."""

    ticker: str
    name: str
    marcap: Decimal  # 시가총액 (원)
    close: Decimal
    equity: Decimal  # 자본총계
    net_income: Decimal  # 당기순이익
    fiscal_year: int


@dataclass(slots=True)
class FactorPick:
    ticker: str
    name: str
    pbr: float
    net_income: Decimal
    marcap: Decimal
    close: Decimal
    weight: float  # 동일가중 목표 비중


def select_factor_picks(rows: list[FundamentalRow], *, top: int = 10) -> list[FactorPick]:
    """저PBR + 흑자 상위 ``top`` 종목 (PBR 오름차순, 동일가중)."""
    eligible = [
        r for r in rows if r.equity > 0 and r.net_income > 0 and r.marcap > 0
    ]
    eligible.sort(key=lambda r: float(r.marcap / r.equity))
    picked = eligible[:top]
    if not picked:
        return []
    weight = 1.0 / len(picked)
    return [
        FactorPick(
            ticker=r.ticker, name=r.name, pbr=round(float(r.marcap / r.equity), 3),
            net_income=r.net_income, marcap=r.marcap, close=r.close, weight=weight,
        )
        for r in picked
    ]


def parse_single_account(raw: bytes) -> tuple[Decimal, Decimal] | None:
    """fnlttSinglAcnt 응답 → (자본총계, 당기순이익). CFS 우선, 없으면 OFS.

    금액은 콤마 포함 문자열("402,192,070,000,000"), 손실은 음수("-1,234").
    당기순이익은 부분일치 ("당기순이익(손실)" 등). 재무 미공시면 None.
    """
    data: dict[str, Any] = json.loads(raw)
    if str(data.get("status")) != "000":
        return None
    rows = data.get("list") or []
    for fs_div in ("CFS", "OFS"):
        equity: Decimal | None = None
        income: Decimal | None = None
        for r in rows:
            if str(r.get("fs_div")) != fs_div:
                continue
            name = str(r.get("account_nm", "")).strip()
            amount = _parse_amount(str(r.get("thstrm_amount", "")))
            if amount is None:
                continue
            if name == "자본총계":
                equity = amount
            # 부분일치: "당기순이익(손실)" 표기 회사 누락 방지. 앞선 행 우선.
            elif "당기순이익" in name and income is None:
                income = amount
        if equity is not None and income is not None:
            return equity, income
    return None


def _parse_amount(text: str) -> Decimal | None:
    cleaned = text.replace(",", "").strip()
    if not cleaned or cleaned == "-":
        return None
    try:
        return Decimal(cleaned)
    except ArithmeticError:
        return None


class DartFundamentals:
    """DART 단일회사 주요계정(fnlttSinglAcnt) 조회 — corp_code 캐시 공유."""

    def __init__(
        self,
        api_key: str,
        *,
        cache_dir: str | Path = "data",
        transport: Transport | None = None,
    ) -> None:
        self._key = api_key
        self._cache = Path(cache_dir) / "dart_corp_codes.json"
        self._transport = transport or self._default_transport
        self._map: dict[str, str] | None = None  # stock_code -> corp_code
        self._log = get_logger("dart_fundamentals")

    async def fetch(self, stock_code: str, year: int) -> tuple[Decimal, Decimal] | None:
        """(자본총계, 당기순이익) — 미상장/미공시/조회실패는 None (해당 종목 제외)."""
        try:
            corp = await self._corp_code(stock_code)
            if corp is None:
                return None
            raw = await self._transport(f"{_BASE}/fnlttSinglAcnt.json", {
                "crtfc_key": self._key, "corp_code": corp,
                "bsns_year": str(year), "reprt_code": _ANNUAL_REPORT,
            })
            return parse_single_account(raw)
        except Exception:
            self._log.warning("dart_fundamentals.fetch_failed", ticker=stock_code)
            return None

    async def _corp_code(self, stock_code: str) -> str | None:
        if self._map is None:
            if self._cache.exists():
                self._map = json.loads(self._cache.read_text())
            else:
                from ..news.risk import DartRiskChecker

                raw = await self._transport(f"{_BASE}/corpCode.xml", {"crtfc_key": self._key})
                self._map = DartRiskChecker.parse_corp_codes(raw)
                self._cache.parent.mkdir(parents=True, exist_ok=True)
                self._cache.write_text(json.dumps(self._map))
                self._log.info("dart_fundamentals.corp_map_cached", entries=len(self._map))
        return self._map.get(stock_code)

    @staticmethod
    async def _default_transport(url: str, params: dict[str, str]) -> bytes:
        from ..news.risk import dart_http_get

        return await dart_http_get(url, params)  # 키가 담긴 URL 을 예외에 싣지 않는 공용 경로
