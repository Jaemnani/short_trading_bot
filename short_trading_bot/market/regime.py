"""시장 레짐(체제) 필터 — "시장 날씨가 나쁜 날은 신규 매수를 멈춘다".

규칙 2개 (단순할수록 과최적화 위험이 낮다):
  1. 일 단위: 전일 코스피 종가가 20일선 아래 → 오늘 하루 신규 매수 금지.
     (계산은 serve의 일일 갱신 잡이 FDR 일봉으로 수행해 ``set_daily``로 주입)
  2. 장중: 지수가 당일 -daily_drop_pct 이하로 밀리면 그 시점부터 신규 매수 금지.
     지수는 구독 중인 프록시 ETF(기본 122630, 코스피 2x) 가격으로 대리 측정 —
     ETF 등락률 / 레버리지 배수 ≈ 지수 등락률.

청산·손절은 절대 막지 않는다 — 이 필터는 오직 신규 진입(ENTER/ADD)만 가른다.
``regime_filter=True``인 템플릿(스캐너·ORB 등)에만 적용되고, 자체 레짐 게이트가
있는 눌림목 계열은 건드리지 않는다 (검증된 기존 동작 유지 원칙).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ..infra.logging import get_logger


class MarketRegime:
    def __init__(
        self,
        *,
        proxy_ticker: str = "122630",
        proxy_leverage: float = 2.0,
        daily_drop_pct: float = 0.015,
    ) -> None:
        self.proxy_ticker = proxy_ticker
        self._leverage = proxy_leverage
        self._drop = daily_drop_pct
        self._daily_ok = True  # 전일 종가 > MA20 (일일 갱신 잡이 주입)
        self._intraday_ok = True  # 당일 급락 플래그 (그날 안에서는 sticky)
        self._proxy_prev_close: Decimal | None = None
        self._proxy_last_close: Decimal | None = None
        self._day: date | None = None
        self._log = get_logger("regime")

    @property
    def entries_allowed(self) -> bool:
        return self._daily_ok and self._intraday_ok

    def set_daily(self, ok: bool, *, proxy_prev_close: Decimal | None = None) -> None:
        """일일 갱신: 전일 코스피 종가 vs 20일선 판정 결과 (+ 프록시 전일 종가)."""
        if ok != self._daily_ok:
            self._log.info("regime.daily", entries_allowed=ok)
        self._daily_ok = ok
        if proxy_prev_close is not None and proxy_prev_close > 0:
            self._proxy_prev_close = proxy_prev_close

    def on_proxy_bar(self, day: date, close: Decimal) -> None:
        """프록시 ETF 봉마다 호출 — 당일 지수 등락률을 대리 계산."""
        if day != self._day:
            self._day = day
            self._intraday_ok = True
            # 새 거래일: 전일 종가 = 직전 거래일 마지막 봉. (일일 갱신 잡이 나중에
            # FDR 공식 종가로 덮어쓴다 — 어느 쪽이 먼저여도 수렴.)
            if self._proxy_last_close is not None:
                self._proxy_prev_close = self._proxy_last_close
        self._proxy_last_close = close
        if not self._intraday_ok or self._proxy_prev_close is None or self._proxy_prev_close <= 0:
            return
        index_change = float(close / self._proxy_prev_close - 1) / self._leverage
        if index_change <= -self._drop:
            self._intraday_ok = False  # 그날은 회복해도 재개하지 않는다 (변동성 장 재진입 방지)
            self._log.info("regime.intraday_drop", index_change=round(index_change, 4))

    def new_day_reset(self) -> None:
        """프록시 봉이 없는 날(휴장 직후 등)의 방어적 리셋."""
        self._intraday_ok = True
