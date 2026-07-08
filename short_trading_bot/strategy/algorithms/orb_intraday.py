"""orb_intraday_v1 — 시가 레인지 돌파(ORB) 단타 알고리즘 (intraday-only, long-only).

Rules:
- Opening range (OR) = the first ``or_minutes`` of the session (default 09:00~09:30 KST).
- ENTER when a post-OR bar CLOSES above the OR high, confirmed by volume (RVOL) and an
  optional VWAP filter (close > VWAP = buyers in control). One entry per day by default.
- Stop = max(OR low, entry - atr_stop_mult*ATR) — whichever is tighter. TP ladder and the
  chandelier trail reuse the lot-level ``PositionParams`` config.
- HARD session flat: everything is force-exited at ``flat_by`` (default 15:10 KST, before
  the 15:20 closing auction). No overnight positions, ever.

Notes:
- Bar timestamps are converted to KST for session logic (the feed emits UTC instants).
- OR accumulates only from bars seen inside the window; if the engine (re)starts mid-window
  the day's OR is partial/absent and the strategy simply stays flat — conservative by design.
- warmup_bars=16 (ATR(14) ready). RVOL(20) may be None early on day 1; the volume filter
  then falls back to comparing against the session's bars seen so far.
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


class OrbParams(BaseModel):
    or_minutes: int = Field(default=30, ge=5, le=120)  # opening-range length
    session_open: str = "09:00"  # KST
    entry_cutoff: str = "14:00"  # no NEW entries after this (KST)
    flat_by: str = "15:10"  # force-exit everything at/after this (before 15:20 auction)
    min_rvol: float = Field(default=1.5, ge=0)
    use_vwap_filter: bool = True
    atr_stop_mult: float = Field(default=1.5, gt=0)
    max_entries_per_day: int = Field(default=1, ge=1)
    news_block: float = -0.3

    @field_validator("session_open", "entry_cutoff", "flat_by")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError(f"expected HH:MM, got {v!r}")
        return v


@register_strategy("orb_intraday_v1")
class OrbIntraday(Strategy):
    meta: ClassVar[StrategyMeta] = StrategyMeta(
        id="orb_intraday_v1",
        name="ORB 시가돌파 단타",
        version="1",
        description=(
            "장 시작 레인지(기본 30분) 상향 돌파 시 거래량·VWAP 확인 후 진입, "
            "ATR/레인지 하단 손절, 장 마감 전 무조건 전량 청산(오버나이트 없음)."
        ),
        supported_resolutions=[
            Resolution.M1, Resolution.M3, Resolution.M5, Resolution.M10, Resolution.M15
        ],
    )
    ParamsModel: ClassVar[type[BaseModel]] = OrbParams

    def __init__(self, params: BaseModel) -> None:
        super().__init__(params)
        self._day: date | None = None
        self._or_high: Decimal | None = None
        self._or_low: Decimal | None = None
        self._entries_today = 0

    @property
    def warmup_bars(self) -> int:
        return 16  # ATR(14); RVOL may arrive later (see module notes)

    # -- session/state helpers --------------------------------------------

    def _roll_day(self, day: date) -> None:
        if day != self._day:
            self._day = day
            self._or_high = None
            self._or_low = None
            self._entries_today = 0

    def _observe(self, ctx: StrategyContext, minute: int, p: OrbParams) -> None:
        open_min = _minutes(p.session_open)
        if open_min <= minute < open_min + p.or_minutes:
            high = ctx.snapshot.high if ctx.snapshot.high > 0 else ctx.snapshot.close
            low = ctx.snapshot.low if ctx.snapshot.low > 0 else ctx.snapshot.close
            self._or_high = high if self._or_high is None else max(self._or_high, high)
            self._or_low = low if self._or_low is None else min(self._or_low, low)

    def _or_complete(self, minute: int, p: OrbParams) -> bool:
        return self._or_high is not None and minute >= _minutes(p.session_open) + p.or_minutes

    # -- contract ----------------------------------------------------------

    def evaluate(self, ctx: StrategyContext) -> list[Intent]:
        p: OrbParams = self.params  # type: ignore[assignment]
        ts = ctx.snapshot.ts.astimezone(KST)
        self._roll_day(ts.date())
        minute = ts.hour * 60 + ts.minute
        self._observe(ctx, minute, p)

        if ctx.state in (PositionState.HOLDING, PositionState.SCALING):
            return self._manage(ctx, minute, p)
        if ctx.state is PositionState.WATCHING:
            return self._entry(ctx, minute, p)
        return _hold("noop")

    # -- entry (매수) -------------------------------------------------------

    def _entry(self, ctx: StrategyContext, minute: int, p: OrbParams) -> list[Intent]:
        if not self._or_complete(minute, p):
            return _hold("or_forming")
        if minute >= _minutes(p.entry_cutoff):
            return _hold("entry_cutoff")
        if self._entries_today >= p.max_entries_per_day:
            return _hold("daily_entry_limit")
        if ctx.news_ewma is not None and ctx.news_ewma < p.news_block:
            return _hold("news_negative")

        assert self._or_high is not None and self._or_low is not None
        close = ctx.snapshot.close
        if close <= self._or_high:
            return _hold("below_or_high")

        # Volume confirmation: RVOL when available, else the bar must beat the recent mean.
        rvol = ctx.ind("rvol")
        if rvol is not None and rvol < p.min_rvol:
            return _hold("rvol_low")

        vwap = ctx.ind("vwap")
        if p.use_vwap_filter and vwap is not None and float(close) < vwap:
            return _hold("below_vwap")

        stop = self._initial_stop(ctx, close, p)
        if stop is None or stop >= close:
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
                stop_price=stop, reason="orb_breakout",
            )
        ]

    def _initial_stop(self, ctx: StrategyContext, entry: Decimal, p: OrbParams) -> Decimal | None:
        assert self._or_low is not None
        atr = ctx.ind("atr_14")
        candidates = [self._or_low]
        if atr is not None and atr > 0:
            candidates.append(entry - Decimal(str(p.atr_stop_mult)) * Decimal(str(atr)))
        stop = max(candidates)  # tighter of OR-low / ATR stop
        return stop if 0 < stop < entry else None

    # -- manage (보유 중) ----------------------------------------------------

    def _manage(self, ctx: StrategyContext, minute: int, p: OrbParams) -> list[Intent]:
        # 1) HARD session flat — 단타의 제1원칙: 오버나이트 금지.
        if minute >= _minutes(p.flat_by):
            return [Intent(IntentKind.EXIT, Side.SELL, reason="session_end")]

        close = float(ctx.snapshot.close)

        # 2) Hard stop.
        if ctx.initial_stop is not None and ctx.snapshot.close <= ctx.initial_stop:
            return [Intent(IntentKind.EXIT, Side.SELL, reason="hard_stop")]

        # 3) Chandelier trailing stop (lot-level stop config).
        atr = ctx.ind("atr_14")
        if ctx.params.stop.use_trailing and atr is not None:
            trail = float(chandelier_stop(ctx.peak_price, atr, ctx.params.stop.chandelier_mult))
            if close <= trail:
                return [Intent(IntentKind.EXIT, Side.SELL, reason="trailing_stop")]

        # 4) Take-profit ladder (fraction of ORIGINAL position, absolute qty).
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
