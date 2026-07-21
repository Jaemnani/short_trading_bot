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

    # 거래량 조건: 거래량이 줄어드는 반등에는 진입하지 않는다.
    require_vol_expansion: bool = True  # 진입봉 거래량 > 직전봉 거래량 (수축→확장 전환)
    min_rvol: float = Field(default=0.8, ge=0)  # 진입봉 RVOL 하한 (0 = off)

    # 레짐 적응 리스크: 강세 레짐(ADX≥bull_adx_min, +DI>-DI)에서 리스크를 배수로 상향.
    bull_risk_mult: float = Field(default=1.5, ge=1.0, le=3.0)  # 1.0 = off
    bull_adx_min: float = Field(default=25.0, ge=0, le=100)

    # 장기 추세 확인: 종가 > MA120 요구 (하락장 반짝 반등 = '가짜 상승추세' 차단).
    # 25종목 검증에서 손실은 전부 장기 하락/횡보 종목의 베어랠리 진입이었다. False=기존 동작.
    require_above_sma120: bool = False
    # 변동성 상한: ATR/종가가 이 값 초과 종목은 진입 스킵 (수직 급등주는 눌림이 깊어
    # 손절이 반복 — 두산에너빌 -27% 사례). None=끔(기존 동작).
    max_atr_pct: float | None = Field(default=None, gt=0, le=0.20)

    # 분할매수(피라미딩): +add_trigger_r 이상 수익 중 새 눌림목 셋업에서만 추가 (승자에만 불타기).
    max_adds: int = Field(default=1, ge=0, le=3)  # 0 = off
    add_fraction: float = Field(default=0.5, gt=0, le=1.0)  # 원 수량 대비 추가 크기
    add_trigger_r: float = Field(default=0.5, ge=0)  # 최소 +0.5R 이익 중일 때만


@register_strategy("pullback_daily_v1")
class PullbackDaily(Strategy):
    meta: ClassVar[StrategyMeta] = StrategyMeta(
        id="pullback_daily_v1",
        name="눌림목 매수 (일봉)",
        version="1",
        description=(
            "상승추세(종가>MA60, MA20>MA60) 종목이 20선까지 눌렸다가 반등(직전봉 대비 상승, "
            "RSI 40~60)할 때 매수. ATR 손절 + 트레일링 + 익절 래더, MA60 이탈 시 청산. "
            "일봉·60분봉 지원(로직은 봉 개수 기준)."
        ),
        supported_resolutions=[Resolution.M60, Resolution.D1],
    )
    ParamsModel: ClassVar[type[BaseModel]] = PullbackParams

    def __init__(self, params: BaseModel) -> None:
        super().__init__(params)
        self._adds = 0  # pyramid adds used in this lot's lifetime

    @property
    def warmup_bars(self) -> int:
        return 61  # MA60 + 1

    # -- shared setup check (entry & pyramid add) -----------------------------

    def _setup_fail(self, ctx: StrategyContext, p: PullbackParams) -> str | None:
        """Return a fail reason for the pullback setup, or None when it's valid."""
        sma20, sma60, rsi = ctx.ind("sma_20"), ctx.ind("sma_60"), ctx.ind("rsi_14")
        if sma20 is None or sma60 is None or rsi is None:
            return "warming_up"
        close = float(ctx.snapshot.close)
        low = float(ctx.snapshot.low if ctx.snapshot.low > 0 else ctx.snapshot.close)
        if not (close > sma60 and sma20 > sma60):
            return "no_uptrend"
        if p.require_above_sma120:
            sma120 = ctx.ind("sma_120")
            if sma120 is None or close <= sma120:
                return "below_long_ma"
        if p.max_atr_pct is not None:
            atr = ctx.ind("atr_14")
            if atr is not None and close > 0 and atr / close > p.max_atr_pct:
                return "too_volatile"
        if low > sma20 * (1 + p.touch_band_pct):
            return "no_pullback"
        if close < sma20 * (1 - p.max_below_pct):
            return "broke_ma20"
        if not (p.rsi_min <= rsi <= p.rsi_max):
            return "rsi_out_of_zone"
        if p.require_turn_up:
            prev_close = float(ctx.prev.close) if ctx.prev is not None else None
            if prev_close is None or close <= prev_close:
                return "no_turn_up"
        # 거래량: 반등봉의 거래량이 직전봉보다 줄었거나(수축 지속) 평균 대비 빈약하면 스킵.
        if p.require_vol_expansion and ctx.prev is not None and ctx.prev.volume > 0:
            if ctx.snapshot.volume <= ctx.prev.volume:
                return "volume_declining"
        rvol = ctx.ind("rvol")
        if p.min_rvol > 0 and rvol is not None and rvol < p.min_rvol:
            return "volume_declining"
        if ctx.news_ewma is not None and ctx.news_ewma < p.news_block:
            return "news_negative"
        return None

    def _strong_regime(self, ctx: StrategyContext, p: PullbackParams) -> bool:
        """강세 레짐: 추세 강도(ADX)와 방향(+DI>-DI)이 확인될 때 리스크 상향."""
        adx, pdi, mdi = ctx.ind("adx_14"), ctx.ind("plus_di"), ctx.ind("minus_di")
        return (
            adx is not None and adx >= p.bull_adx_min
            and pdi is not None and mdi is not None and pdi > mdi
        )

    def evaluate(self, ctx: StrategyContext) -> list[Intent]:
        if ctx.state in (PositionState.HOLDING, PositionState.SCALING):
            return self._manage(ctx)
        if ctx.state is PositionState.WATCHING:
            return self._entry(ctx)
        return _hold("noop")

    # -- entry (눌림목 매수) --------------------------------------------------

    def _entry(self, ctx: StrategyContext) -> list[Intent]:
        p: PullbackParams = self.params  # type: ignore[assignment]
        fail = self._setup_fail(ctx, p)
        if fail is not None:
            return _hold(fail)

        entry = ctx.snapshot.close
        stop = self._initial_stop(ctx, entry)
        if stop is None:
            return _hold("no_stop")

        # 레짐 적응 리스크: 강세 레짐에서 리스크를 bull_risk_mult배로 상향.
        strong = p.bull_risk_mult > 1.0 and self._strong_regime(ctx, p)
        risk = ctx.params.risk_per_trade * (p.bull_risk_mult if strong else 1.0)
        qty = risk_based_qty(
            ctx.equity, risk, entry, stop,
            allow_fractional=ctx.params.market.is_overseas,
        )
        if qty <= 0:
            return _hold("size_zero")
        reason = "pullback_buy_bull" if strong else "pullback_buy"
        return [Intent(kind=IntentKind.ENTER, side=Side.BUY, qty=qty, stop_price=stop, reason=reason)]

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

        add = self._pyramid_add(ctx, close)
        if add is not None:
            return [add]

        return _hold("holding")

    def _pyramid_add(self, ctx: StrategyContext, close: float) -> Intent | None:
        """분할매수: 최소 +add_trigger_r R 이익 중 + 새 눌림목 셋업일 때만 추가 (물타기 금지)."""
        p: PullbackParams = self.params  # type: ignore[assignment]
        if p.max_adds <= 0 or self._adds >= p.max_adds:
            return None
        if ctx.initial_stop is None or ctx.original_qty <= 0:
            return None
        risk0 = float(ctx.avg_entry) - float(ctx.initial_stop)
        if risk0 <= 0 or close < float(ctx.avg_entry) + p.add_trigger_r * risk0:
            return None  # 이익 중이 아니면 절대 추가하지 않음
        if self._setup_fail(ctx, p) is not None:
            return None  # 새 눌림목-반등 셋업에서만
        raw = ctx.original_qty * Decimal(str(p.add_fraction))
        qty = raw if ctx.params.market.is_overseas else raw.to_integral_value(rounding=ROUND_DOWN)
        if qty <= 0:
            return None
        self._adds += 1
        return Intent(kind=IntentKind.ADD, side=Side.BUY, qty=qty, reason="pyramid_add")

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
