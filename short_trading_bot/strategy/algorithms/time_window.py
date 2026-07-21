"""time_window_v1 — 시간대·요일 패턴 단타 (intraday-only, long-only).

'요일별·시간별 평균 흐름'을 조건부로 거래하는 범용 시간창 전략. 10년 지수 검증에서
무조건부 달력 매매(요일 고정 매수, 오버나이트 등)는 왕복비용(~0.14%)을 이기지 못했고,
유일하게 살아남은 조합이 "오전 하락일의 오후 되돌림"이라 그것이 기본값이다:

  기본값: 오전(시가→entry_time)에 morning_return_max 이하로 하락한 날만,
  entry_time(14:00)~entry_end(14:10) 사이 매수 → flat_by(15:20) 무조건 청산.
  손절은 ATR 배수. 검증: KODEX레버리지(122630) 1분봉 12개월 — ATR 2.5x에서 +5.5%
  (승률 31%, MDD 9.3%)이나 손절 폭 민감(1.5x -4.5%, 4x +2.1%, 8x -3.7%)하고
  폭락 2주(-59만) 취약. 코스닥레버(233740)는 마이너스. ⚠️ 표본 1년뿐 —
  실전 부적격, 페이퍼 관찰 전용 (2026-07-21 판정).

요일 마스크·시간창·조건을 바꿔 다른 달력 패턴도 실험할 수 있다 (weekdays=[0]로
월요일만 등). 오버나이트 보유는 지원하지 않는다 — 10년 검증에서 2018 -48%/2022 -59%
(레버 2x, 비용 후)로 기각됐고 갭 리스크는 손절이 불가능하다.
"""

from __future__ import annotations

import re
from datetime import date, timedelta, timezone
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator

from ...domain.enums import PositionState, Resolution, Side
from ...domain.signal import Intent, IntentKind
from ..base import Strategy, StrategyContext, StrategyMeta
from ..registry import register_strategy
from ..sizing import risk_based_qty

KST = timezone(timedelta(hours=9))
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hold(reason: str) -> list[Intent]:
    return [Intent(kind=IntentKind.HOLD, side=Side.BUY, reason=reason)]


class TimeWindowParams(BaseModel):
    entry_time: str = "14:00"  # 진입 시작 (KST)
    entry_end: str = "14:10"  # 진입 마감 — 이 창을 놓치면 그날은 관망
    flat_by: str = "15:20"  # 무조건 전량 청산 (오버나이트 금지)
    # 진입 조건: 시가 대비 entry_time 시점 수익률이 이 값 이하 (오전 하락일만).
    # None = 무조건부 (순수 달력 매매 — 10년 검증상 비추천).
    morning_return_max: float | None = Field(default=-0.002, ge=-0.10, le=0.10)
    weekdays: list[int] = Field(default=[0, 1, 2, 3, 4])  # 0=월 .. 4=금
    # 되돌림 매매라 타이트한 손절은 바닥 출렁임에 털린다 — 12개월 스윕에서 2.5x만 견고.
    atr_stop_mult: float = Field(default=2.5, gt=0)
    news_block: float = -0.3

    @field_validator("entry_time", "entry_end", "flat_by")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError(f"expected HH:MM, got {v!r}")
        return v

    @field_validator("weekdays")
    @classmethod
    def _valid_weekdays(cls, v: list[int]) -> list[int]:
        if not v or any(d < 0 or d > 4 for d in v):
            raise ValueError("weekdays must be non-empty, values 0(월)~4(금)")
        return v


@register_strategy("time_window_v1")
class TimeWindow(Strategy):
    meta: ClassVar[StrategyMeta] = StrategyMeta(
        id="time_window_v1",
        name="시간창 단타 (오후 되돌림)",
        version="1",
        description=(
            "요일·시간창·오전수익률 조건이 맞는 날 지정 시각에 매수, ATR 손절, "
            "마감 전 무조건 청산. 기본값 = 오전 하락일의 오후 되돌림 (코스피 레버 검증)."
        ),
        supported_resolutions=[
            Resolution.M1, Resolution.M3, Resolution.M5, Resolution.M10, Resolution.M15
        ],
    )
    ParamsModel: ClassVar[type[BaseModel]] = TimeWindowParams

    def __init__(self, params: BaseModel) -> None:
        super().__init__(params)
        self._day: date | None = None
        self._session_open: Decimal | None = None
        self._entered_today = False

    @property
    def warmup_bars(self) -> int:
        return 16  # ATR(14)

    def _roll_day(self, day: date, bar_open: Decimal) -> None:
        if day != self._day:
            self._day = day
            self._session_open = bar_open
            self._entered_today = False

    def evaluate(self, ctx: StrategyContext) -> list[Intent]:
        p: TimeWindowParams = self.params  # type: ignore[assignment]
        ts = ctx.snapshot.ts.astimezone(KST)
        # 세션 시가: 당일 첫 봉의 시가가 이상적이나 스냅샷엔 open이 없어 첫 봉 종가로
        # 근사한다 (1분봉에서 오차는 수 bp — morning_return_max 판정엔 충분).
        self._roll_day(ts.date(), ctx.snapshot.close)
        minute = ts.hour * 60 + ts.minute

        if ctx.state in (PositionState.HOLDING, PositionState.SCALING):
            if minute >= _minutes(p.flat_by):
                return [Intent(IntentKind.EXIT, Side.SELL, reason="session_end")]
            if ctx.initial_stop is not None and ctx.snapshot.close <= ctx.initial_stop:
                return [Intent(IntentKind.EXIT, Side.SELL, reason="hard_stop")]
            return _hold("holding")

        if ctx.state is not PositionState.WATCHING:
            return _hold("noop")

        # -- entry ----------------------------------------------------------
        if ts.weekday() not in p.weekdays:
            return _hold("weekday_off")
        if self._entered_today:
            return _hold("daily_entry_limit")
        if not (_minutes(p.entry_time) <= minute < _minutes(p.entry_end)):
            return _hold("outside_window")
        if ctx.news_ewma is not None and ctx.news_ewma < p.news_block:
            return _hold("news_negative")

        close = ctx.snapshot.close
        if p.morning_return_max is not None:
            if self._session_open is None or self._session_open <= 0:
                return _hold("no_session_open")
            morning = float(close) / float(self._session_open) - 1.0
            if morning > p.morning_return_max:
                return _hold("morning_filter")

        atr = ctx.ind("atr_14")
        if atr is None or atr <= 0:
            return _hold("no_atr")
        stop = close - Decimal(str(p.atr_stop_mult)) * Decimal(str(atr))
        if stop <= 0 or stop >= close:
            return _hold("no_stop")

        qty = risk_based_qty(
            ctx.equity, ctx.params.risk_per_trade, close, stop,
            allow_fractional=ctx.params.market.is_overseas,
            cost_buffer_pct=ctx.params.sizing_cost_buffer_pct,
        )
        if qty <= 0:
            return _hold("size_zero")

        self._entered_today = True
        return [
            Intent(
                kind=IntentKind.ENTER, side=Side.BUY, qty=qty,
                stop_price=stop, reason="time_window",
            )
        ]
