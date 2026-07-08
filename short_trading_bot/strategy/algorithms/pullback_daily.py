"""pullback_daily_v1 — 눌림목 매수 (daily-bar swing, long-only).

Rules:
- REGIME: uptrend only — close > MA60 and MA20 > MA60 (완만한 정배열).
- SETUP: price pulls back TO the 20-day MA — the bar's low touches (or comes within
  ``touch_band_pct`` of) MA20 while the close holds above ``max_below_pct`` under it.
- TRIGGER: turn-up confirmation — close > previous close, with RSI in a healthy pullback
  zone [rsi_min, rsi_max] (not collapsed, not already overbought).
- STOP: entry - atr_mult*ATR (falls back to the lot-level StopConfig fixed_pct if set).
- MANAGE: hard stop → chandelier trail → time stop (max_hold_bars) → TP ladder →
  trend-break exit (close < MA60).

Complements trend_long_v1 (breakout) — this buys the dip inside an established uptrend
instead of chasing strength.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import ClassVar

from pydantic import BaseModel, Field

from ...domain.enums import PositionState, Resolution, Side
from ...domain.signal import Intent, IntentKind
from ..base import Strategy, StrategyContext, StrategyMeta
from ..registry import register_strategy
from ..sizing import atr_stop, chandelier_stop, pct_stop, risk_based_qty


def _hold(reason: str) -> list[Intent]:
    return [Intent(kind=IntentKind.HOLD, side=Side.BUY, reason=reason)]


class PullbackParams(BaseModel):
    touch_band_pct: float = Field(default=0.01, ge=0, le=0.05)  # low within 1% of MA20
    max_below_pct: float = Field(default=0.02, ge=0, le=0.10)  # close may dip max 2% under MA20
    rsi_min: float = Field(default=40.0, ge=0, le=100)
    rsi_max: float = Field(default=60.0, ge=0, le=100)
    require_turn_up: bool = True  # close > prev close on the entry bar
    news_block: float = -0.3


@register_strategy("pullback_daily_v1")
class PullbackDaily(Strategy):
    meta: ClassVar[StrategyMeta] = StrategyMeta(
        id="pullback_daily_v1",
        name="눌림목 매수 (일봉)",
        version="1",
        description=(
            "상승추세(종가>MA60, MA20>MA60) 종목이 20일선까지 눌렸다가 반등(전일 대비 상승, "
            "RSI 40~60)할 때 매수. ATR 손절 + 트레일링 + 익절 래더, MA60 이탈 시 청산."
        ),
        supported_resolutions=[Resolution.D1],
    )
    ParamsModel: ClassVar[type[BaseModel]] = PullbackParams

    @property
    def warmup_bars(self) -> int:
        return 61  # MA60 + 1

    def evaluate(self, ctx: StrategyContext) -> list[Intent]:
        if ctx.state in (PositionState.HOLDING, PositionState.SCALING):
            return self._manage(ctx)
        if ctx.state is PositionState.WATCHING:
            return self._entry(ctx)
        return _hold("noop")

    # -- entry (눌림목 매수) --------------------------------------------------

    def _entry(self, ctx: StrategyContext) -> list[Intent]:
        p: PullbackParams = self.params  # type: ignore[assignment]
        sma20, sma60, rsi = ctx.ind("sma_20"), ctx.ind("sma_60"), ctx.ind("rsi_14")
        if sma20 is None or sma60 is None or rsi is None:
            return _hold("warming_up")

        close = float(ctx.snapshot.close)
        low = float(ctx.snapshot.low if ctx.snapshot.low > 0 else ctx.snapshot.close)

        # 1) Regime: established uptrend only.
        if not (close > sma60 and sma20 > sma60):
            return _hold("no_uptrend")

        # 2) Setup: the bar dipped to/near MA20 but the close held.
        if low > sma20 * (1 + p.touch_band_pct):
            return _hold("no_pullback")
        if close < sma20 * (1 - p.max_below_pct):
            return _hold("broke_ma20")

        # 3) Trigger: turning back up, RSI in the healthy-pullback zone.
        if not (p.rsi_min <= rsi <= p.rsi_max):
            return _hold("rsi_out_of_zone")
        if p.require_turn_up:
            prev_close = float(ctx.prev.close) if ctx.prev is not None else None
            if prev_close is None or close <= prev_close:
                return _hold("no_turn_up")

        # 4) News gate.
        if ctx.news_ewma is not None and ctx.news_ewma < p.news_block:
            return _hold("news_negative")

        entry = ctx.snapshot.close
        stop = self._initial_stop(ctx, entry)
        if stop is None:
            return _hold("no_stop")
        qty = risk_based_qty(
            ctx.equity, ctx.params.risk_per_trade, entry, stop,
            allow_fractional=ctx.params.market.is_overseas,
        )
        if qty <= 0:
            return _hold("size_zero")
        return [
            Intent(kind=IntentKind.ENTER, side=Side.BUY, qty=qty, stop_price=stop, reason="pullback_buy")
        ]

    def _initial_stop(self, ctx: StrategyContext, entry: Decimal) -> Decimal | None:
        cfg = ctx.params.stop
        if cfg.fixed_pct is not None:
            stop = pct_stop(entry, cfg.fixed_pct)
        else:
            atr = ctx.ind("atr_14")
            if atr is None or atr <= 0:
                return None
            stop = atr_stop(entry, atr, cfg.atr_mult)
        return stop if 0 < stop < entry else None

    # -- manage (보유 중) ------------------------------------------------------

    def _manage(self, ctx: StrategyContext) -> list[Intent]:
        close = float(ctx.snapshot.close)

        if ctx.initial_stop is not None and ctx.snapshot.close <= ctx.initial_stop:
            return [Intent(IntentKind.EXIT, Side.SELL, reason="hard_stop")]

        atr = ctx.ind("atr_14")
        if ctx.params.stop.use_trailing and atr is not None:
            trail = float(chandelier_stop(ctx.peak_price, atr, ctx.params.stop.chandelier_mult))
            if close <= trail:
                return [Intent(IntentKind.EXIT, Side.SELL, reason="trailing_stop")]

        if ctx.params.max_hold_bars is not None and ctx.bars_held >= ctx.params.max_hold_bars:
            return [Intent(IntentKind.EXIT, Side.SELL, reason="max_hold")]

        tp = self._take_profit(ctx, close)
        if tp is not None:
            return [tp]

        sma60 = ctx.ind("sma_60")
        if sma60 is not None and close < sma60:
            return [Intent(IntentKind.EXIT, Side.SELL, reason="trend_break")]

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
