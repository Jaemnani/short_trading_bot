"""momo_intraday_v1 — 급등(모멘텀) 합류 단타 알고리즘 (intraday-only, long-only).

장중 스캐너가 "상승세 + 거래량 급증(인기)" 종목을 찾아 이 전략의 랏을 동적으로
띄운다는 전제의 진입/이탈 전담 전략. ORB와 달리 시가 레인지가 필요 없어서 장중
아무 때나 합류가 성립한다.

Rules:
- ENTER: 세션 내 + entry_cutoff 이전, RVOL ≥ min_rvol(거래 활발 지속), 종가 > VWAP
  (매수 우위), 직전 봉보다 상승(추세 지속 확인). 하루 max_entries_per_day회.
- Stop: entry - atr_stop_mult x ATR. TP 사다리·샹들리에 트레일은 lot-level
  ``PositionParams`` 설정 재사용.
- 이탈: 하드 스톱 → VWAP 이탈(exit_below_vwap, 모멘텀 소멸) → 트레일 → TP 순으로
  평가, ``flat_by``(기본 15:10 KST)에 무조건 전량 청산 — 오버나이트 금지.

급등주 특성상 늦게 합류할수록 되돌림 리스크가 크다 — entry_cutoff 기본 14:00,
스캐너 쪽에서도 합류 마감을 함께 건다.
"""

from __future__ import annotations

import re
from datetime import date, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator

from ...domain.enums import PositionState, Resolution, Side
from ...domain.signal import Intent, IntentKind
from ..base import Strategy, StrategyContext, StrategyMeta
from ..registry import register_strategy
from ..sizing import chandelier_stop, risk_based_qty

KST = timezone(timedelta(hours=9))
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hold(reason: str) -> list[Intent]:
    return [Intent(kind=IntentKind.HOLD, side=Side.BUY, reason=reason)]


class MomoParams(BaseModel):
    session_open: str = "09:00"  # KST
    entry_cutoff: str = "14:00"  # no NEW entries after this (KST)
    flat_by: str = "15:10"  # force-exit everything (before 15:20 auction)
    min_rvol: float = Field(default=2.0, ge=0)  # 합류 시점에도 거래가 살아있어야 함
    use_vwap_filter: bool = True
    exit_below_vwap: bool = True  # 보유 중 종가<VWAP → 모멘텀 소멸로 청산
    atr_stop_mult: float = Field(default=1.5, gt=0)
    max_entries_per_day: int = Field(default=1, ge=1)
    news_block: float = -0.3

    @field_validator("session_open", "entry_cutoff", "flat_by")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError(f"expected HH:MM, got {v!r}")
        return v


@register_strategy("momo_intraday_v1")
class MomoIntraday(Strategy):
    meta: ClassVar[StrategyMeta] = StrategyMeta(
        id="momo_intraday_v1",
        name="급등 모멘텀 합류 단타",
        version="1",
        description=(
            "스캐너가 찾은 급등+거래량 급증 종목에 VWAP·RVOL 확인 후 합류, "
            "ATR 손절·VWAP 이탈·트레일링으로 이탈, 장 마감 전 무조건 전량 청산."
        ),
        supported_resolutions=[
            Resolution.M1, Resolution.M3, Resolution.M5, Resolution.M10, Resolution.M15
        ],
    )
    ParamsModel: ClassVar[type[BaseModel]] = MomoParams

    def __init__(self, params: BaseModel) -> None:
        super().__init__(params)
        self._day: date | None = None
        self._entries_today = 0

    @property
    def warmup_bars(self) -> int:
        return 16  # ATR(14) ready — 합류 시 당일 분봉 백필로 채운다

    def _roll_day(self, day: date) -> None:
        if day != self._day:
            self._day = day
            self._entries_today = 0

    # -- contract ----------------------------------------------------------

    def evaluate(self, ctx: StrategyContext) -> list[Intent]:
        p: MomoParams = self.params  # type: ignore[assignment]
        ts = ctx.snapshot.ts.astimezone(KST)
        self._roll_day(ts.date())
        minute = ts.hour * 60 + ts.minute

        if ctx.state in (PositionState.HOLDING, PositionState.SCALING):
            return self._manage(ctx, minute, p)
        if ctx.state is PositionState.WATCHING:
            return self._entry(ctx, minute, p)
        return _hold("noop")

    # -- entry (합류) -------------------------------------------------------

    def _entry(self, ctx: StrategyContext, minute: int, p: MomoParams) -> list[Intent]:
        if minute < _minutes(p.session_open):
            return _hold("pre_session")
        if minute >= _minutes(p.entry_cutoff):
            return _hold("entry_cutoff")
        if self._entries_today >= p.max_entries_per_day:
            return _hold("daily_entry_limit")
        if ctx.news_ewma is not None and ctx.news_ewma < p.news_block:
            return _hold("news_negative")

        close = ctx.snapshot.close
        rvol = ctx.ind("rvol")
        if rvol is not None and rvol < p.min_rvol:
            return _hold("rvol_low")

        vwap = ctx.ind("vwap")
        if p.use_vwap_filter and vwap is not None and float(close) < vwap:
            return _hold("below_vwap")

        # 추세 지속 확인: 직전 봉 종가보다 올라야 합류 (하락 전환 봉에서 물리지 않기).
        if ctx.prev is not None and close <= ctx.prev.close:
            return _hold("not_rising")

        atr = ctx.ind("atr_14")
        if atr is None or atr <= 0:
            return _hold("no_atr")
        stop = close - Decimal(str(p.atr_stop_mult)) * Decimal(str(atr))
        if stop <= 0 or stop >= close:
            return _hold("no_stop")

        qty = risk_based_qty(
            ctx.equity, ctx.params.risk_per_trade, close, stop,
            allow_fractional=ctx.params.market.is_overseas,
        )
        if qty <= 0:
            return _hold("size_zero")

        self._entries_today += 1
        return [
            Intent(
                kind=IntentKind.ENTER, side=Side.BUY, qty=qty,
                stop_price=stop, reason="momo_join",
            )
        ]

    # -- manage (보유 중) ----------------------------------------------------

    def _manage(self, ctx: StrategyContext, minute: int, p: MomoParams) -> list[Intent]:
        # 1) HARD session flat — 단타의 제1원칙: 오버나이트 금지.
        if minute >= _minutes(p.flat_by):
            return [Intent(IntentKind.EXIT, Side.SELL, reason="session_end")]

        close = float(ctx.snapshot.close)

        # 2) Hard stop.
        if ctx.initial_stop is not None and ctx.snapshot.close <= ctx.initial_stop:
            return [Intent(IntentKind.EXIT, Side.SELL, reason="hard_stop")]

        # 3) 모멘텀 소멸: VWAP 아래로 마감하면 급등 논리가 깨진 것 — 미련 없이 이탈.
        vwap = ctx.ind("vwap")
        if p.exit_below_vwap and vwap is not None and close < vwap:
            return [Intent(IntentKind.EXIT, Side.SELL, reason="vwap_lost")]

        # 4) Chandelier trailing stop (lot-level stop config).
        atr = ctx.ind("atr_14")
        if ctx.params.stop.use_trailing and atr is not None:
            trail = float(chandelier_stop(ctx.peak_price, atr, ctx.params.stop.chandelier_mult))
            if close <= trail:
                return [Intent(IntentKind.EXIT, Side.SELL, reason="trailing_stop")]

        # 5) Take-profit ladder (fraction of ORIGINAL position, absolute qty).
        tp = self._take_profit(ctx, close)
        if tp is not None:
            return [tp]

        return _hold("holding")

    def _take_profit(self, ctx: StrategyContext, close: float) -> Intent | None:
        rungs = ctx.params.take_profit
        if ctx.initial_stop is None or ctx.tp_rungs_taken >= len(rungs):
            return None
        risk = float(ctx.avg_entry) - float(ctx.initial_stop)
        if risk <= 0:
            return None
        rung = rungs[ctx.tp_rungs_taken]
        if close < float(ctx.avg_entry) + rung.r_multiple * risk:
            return None
        reason = f"take_profit_{ctx.tp_rungs_taken + 1}"
        if ctx.original_qty > 0:
            raw = ctx.original_qty * Decimal(str(rung.fraction))
            qty = raw if ctx.params.market.is_overseas else raw.to_integral_value(rounding=ROUND_DOWN)
            qty = min(qty, ctx.qty) if ctx.qty > 0 else raw
            if qty <= 0:
                qty = ctx.qty
            return Intent(kind=IntentKind.TRIM, side=Side.SELL, qty=qty, reason=reason)
        return Intent(kind=IntentKind.TRIM, side=Side.SELL, fraction=rung.fraction, reason=reason)
