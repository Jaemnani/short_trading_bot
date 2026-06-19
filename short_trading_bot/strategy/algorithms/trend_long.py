"""trend_long_v1 — the first built-in algorithm (plugin example).

3-layer long entry (regime gate -> trigger -> confirm) + news overlay, ATR initial stop,
Chandelier trailing stop, and a take-profit ladder. One of many; add more the same way.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import cast

from pydantic import BaseModel

from ...domain.enums import PositionState, Side
from ...domain.signal import Intent, IntentKind
from ..base import Strategy, StrategyContext, StrategyMeta
from ..registry import register_strategy
from ..rules import blocks
from ..sizing import atr_stop, chandelier_stop, pct_stop, risk_based_qty


def _hold(reason: str) -> list[Intent]:
    return [Intent(kind=IntentKind.HOLD, side=Side.BUY, reason=reason)]


@register_strategy("trend_long_v1")
class TrendLongV1(Strategy):
    meta = StrategyMeta(
        id="trend_long_v1",
        name="Trend Long v1",
        description="3-layer regime/trigger/confirm long; ATR + chandelier stop; TP ladder.",
    )

    class Params(BaseModel):
        adx_min: float = 25.0
        rsi_low: float = 50.0
        rsi_high: float = 70.0
        rvol_min: float = 1.5
        require_full_ma_alignment: bool = False
        require_confirm: bool = True  # need >=1 of momentum/volume
        news_block: float = -0.3  # block new entries when sentiment EWMA below this

    ParamsModel = Params

    @property
    def _p(self) -> TrendLongV1.Params:
        return cast(TrendLongV1.Params, self.params)

    def evaluate(self, ctx: StrategyContext) -> list[Intent]:
        if ctx.state is PositionState.WATCHING:
            return self._entry(ctx)
        if ctx.state in (PositionState.HOLDING, PositionState.SCALING):
            return self._manage(ctx)
        return _hold("inactive_state")

    # -- entry -----------------------------------------------------------

    def _entry(self, ctx: StrategyContext) -> list[Intent]:
        p = self._p
        snap, prev = ctx.snapshot, ctx.prev

        if ctx.news_ewma is not None and ctx.news_ewma < p.news_block:
            return _hold("news_block")
        if not blocks.regime_bullish(
            snap, adx_min=p.adx_min, require_full_alignment=p.require_full_ma_alignment
        ):
            return _hold("regime_fail")
        if not (
            blocks.macd_cross_up(snap, prev)
            or blocks.golden_cross(snap, prev)
            or blocks.breakout_high(snap, prev)
        ):
            return _hold("no_trigger")

        mom = blocks.momentum_ok(snap, rsi_low=p.rsi_low, rsi_high=p.rsi_high)
        vol = blocks.volume_ok(snap, rvol_min=p.rvol_min)
        if p.require_confirm and not (mom or vol):
            return _hold("no_confirm")

        entry = snap.close
        stop = self._initial_stop(ctx, entry)
        if stop is None:
            return _hold("no_stop")  # cannot size without a stop (needs ATR)

        fractional = ctx.params.market.is_overseas
        base_qty = risk_based_qty(
            ctx.equity, ctx.params.risk_per_trade, entry, stop, allow_fractional=fractional
        )
        qty = self._size(base_qty, mom and vol, ctx.news_ewma, allow_fractional=fractional)
        if qty <= 0:
            return _hold("size_zero")

        return [
            Intent(
                kind=IntentKind.ENTER,
                side=Side.BUY,
                qty=qty,
                stop_price=stop,
                reason="entry",
            )
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
        # A stop must be strictly below entry and positive, else sizing/R-math break.
        return stop if 0 < stop < entry else None

    def _size(
        self,
        base_qty: Decimal,
        full_confirm: bool,
        news_ewma: float | None,
        *,
        allow_fractional: bool,
    ) -> Decimal:
        factor = Decimal("1") if full_confirm else Decimal("0.5")  # partial if not both confirms
        if news_ewma is not None:
            factor *= Decimal(str(max(0.0, min(2.0, 1.0 + news_ewma))))  # news size multiplier
        scaled = base_qty * factor
        if allow_fractional:
            return scaled
        floored = scaled.to_integral_value(rounding=ROUND_DOWN)
        # Don't silently drop a fundable setup to 0 purely from confirm/news rounding.
        if floored <= 0 and base_qty >= 1 and factor > 0:
            return Decimal(1)
        return floored

    # -- manage ----------------------------------------------------------

    def _manage(self, ctx: StrategyContext) -> list[Intent]:
        snap, prev = ctx.snapshot, ctx.prev
        close = float(snap.close)
        atr = ctx.ind("atr_14")

        # 1) Hard stop.
        if ctx.initial_stop is not None and close <= float(ctx.initial_stop):
            return [Intent(IntentKind.EXIT, Side.SELL, reason="stop_loss")]

        # 2) Chandelier trailing stop.
        if ctx.params.stop.use_trailing and atr is not None:
            trail = float(chandelier_stop(ctx.peak_price, atr, ctx.params.stop.chandelier_mult))
            if close <= trail:
                return [Intent(IntentKind.EXIT, Side.SELL, reason="trailing_stop")]

        # 3) Time stop.
        if ctx.params.max_hold_bars is not None and ctx.bars_held >= ctx.params.max_hold_bars:
            return [Intent(IntentKind.EXIT, Side.SELL, reason="max_hold")]

        # 4) Take-profit ladder (one rung per evaluation).
        tp = self._take_profit(ctx, close)
        if tp is not None:
            return [tp]

        # 5) Indicator exits (close the remainder).
        reasons = blocks.exit_reasons(snap, prev)
        if reasons:
            return [Intent(IntentKind.EXIT, Side.SELL, reason=reasons[0])]

        return _hold("hold")

    def _take_profit(self, ctx: StrategyContext, close: float) -> Intent | None:
        rungs = ctx.params.take_profit
        if ctx.initial_stop is None or ctx.tp_rungs_taken >= len(rungs):
            return None
        risk = float(ctx.avg_entry) - float(ctx.initial_stop)
        if risk <= 0:
            return None
        rung = rungs[ctx.tp_rungs_taken]
        target = float(ctx.avg_entry) + rung.r_multiple * risk
        if close < target:
            return None
        reason = f"take_profit_{ctx.tp_rungs_taken + 1}"
        # Sell an ABSOLUTE qty = fraction of the ORIGINAL position (avoids the
        # fraction-of-current vs fraction-of-original ambiguity).
        if ctx.original_qty > 0:
            raw = ctx.original_qty * Decimal(str(rung.fraction))
            qty = raw if ctx.params.market.is_overseas else raw.to_integral_value(rounding=ROUND_DOWN)
            if ctx.qty > 0:
                qty = min(qty, ctx.qty)
            if qty <= 0:  # rounded to nothing -> close the small remainder
                qty = ctx.qty
            return Intent(kind=IntentKind.TRIM, side=Side.SELL, qty=qty, reason=reason)
        return Intent(kind=IntentKind.TRIM, side=Side.SELL, fraction=rung.fraction, reason=reason)
