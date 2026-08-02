"""crash_momentum_v1 — 폭락 다음날 인버스 ETF 단기 모멘텀 (D1 전용, 소액 관찰용).

10년 검증(2016~2026, H1 가설 사전등록 → 통과)의 유일한 플러스 숏 계열:
"코스피 -3% 이상 마감 → 익일 인버스 ETF 매수, 3거래일 보유 후 청산."
34회 발생, 회당 +0.4%(1x)/+1.0%(2x), 누적 +10~18%. 급락 '가속' 구간만 잡기 때문에
상시 인버스 보유(-51%)·레짐 인버스(-73%)와 달리 되돌림 휩쏘를 피한다.

⚠️ 얇은 엣지 + 최악 1회 -20% (연쇄 폭락이 되돌림 없이 이어진 경우) — 소액 전용.
사이징이 리스크 기반이 아니라 자본의 고정 비율(alloc_pct, 기본 5%)인 이유:
검증 시나리오가 손절 없는 단순 보유였고, 손절을 붙이면 검증한 것과 다른 전략이
된다. stop_loss_pct는 옵션으로만 제공한다 (기본 꺼짐 = 검증된 동작).

트리거는 인버스 ETF '자체'의 일수익률로 판정한다 (전략은 자기 종목 봉만 보는 구조).
1배 인버스(114800)는 코스피 -3% ≈ ETF +3%이므로 기본 문턱 +2.8%(추적오차 여유).
2배(252670)에 쓰려면 trigger_ret_pct를 ~0.056으로 올려야 한다.

라이브 D1 봉은 다음 거래일 첫 틱에 확정되므로, 폭락일 봉 평가 = 익일 개장 직후
매수가 되어 검증 시나리오("익일 매수")와 타이밍이 일치한다.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import ClassVar

from pydantic import BaseModel, Field

from ...domain.enums import PositionState, Resolution, Side
from ...domain.signal import Intent, IntentKind
from ..base import Strategy, StrategyContext, StrategyMeta
from ..registry import register_strategy


def _hold(reason: str) -> list[Intent]:
    return [Intent(kind=IntentKind.HOLD, side=Side.BUY, reason=reason)]


class CrashMomentumParams(BaseModel):
    # 자기 봉 일수익률 트리거 — 1x 인버스 기준 +2.8% ≈ 코스피 -3% (추적오차 여유).
    trigger_ret_pct: float = Field(default=0.028, gt=0, le=0.20)
    hold_days: int = Field(default=3, ge=1, le=10)  # 진입 후 보유 거래일 수
    alloc_pct: float = Field(default=0.05, gt=0, le=0.30)  # 자본 대비 투입 비율 (소액)
    # 옵션 손절 (진입가 대비 하락률). 기본 None = 검증된 단순 보유 그대로.
    stop_loss_pct: float | None = Field(default=None, gt=0, le=0.50)


@register_strategy("crash_momentum_v1")
class CrashMomentum(Strategy):
    meta: ClassVar[StrategyMeta] = StrategyMeta(
        id="crash_momentum_v1",
        name="폭락 모멘텀 (익일 인버스)",
        version="1",
        description=(
            "지수 폭락 마감(인버스 ETF 급등)을 확인하고 다음날 인버스를 매수, "
            "3거래일 보유 후 청산. 10년 34회·유일한 플러스 숏 — 소액 관찰용."
        ),
        supported_resolutions=[Resolution.D1],
    )
    ParamsModel: ClassVar[type[BaseModel]] = CrashMomentumParams

    @property
    def warmup_bars(self) -> int:
        return 2  # 전일 봉만 있으면 판정 가능

    def evaluate(self, ctx: StrategyContext) -> list[Intent]:
        p: CrashMomentumParams = self.params  # type: ignore[assignment]

        if ctx.state in (PositionState.HOLDING, PositionState.SCALING):
            if ctx.bars_held >= p.hold_days:
                return [Intent(IntentKind.EXIT, Side.SELL, reason="hold_expiry")]
            if ctx.initial_stop is not None and ctx.snapshot.close <= ctx.initial_stop:
                return [Intent(IntentKind.EXIT, Side.SELL, reason="hard_stop")]
            return _hold("holding")

        if ctx.state is not PositionState.WATCHING:
            return _hold("noop")

        # -- entry: 자기 봉(인버스 ETF) 일수익률이 문턱 이상 = 지수 폭락 마감 --------
        if ctx.prev is None or ctx.prev.close <= 0:
            return _hold("no_prev")
        ret = float(ctx.snapshot.close) / float(ctx.prev.close) - 1.0
        if ret < p.trigger_ret_pct:
            return _hold("no_crash")

        close = ctx.snapshot.close
        if close <= 0 or ctx.equity <= 0:
            return _hold("bad_inputs")
        qty = (ctx.equity * Decimal(str(p.alloc_pct)) / close).to_integral_value(
            rounding=ROUND_DOWN
        )
        if qty <= 0:
            return _hold("size_zero")

        stop: Decimal | None = None
        if p.stop_loss_pct is not None:
            stop = close * (Decimal(1) - Decimal(str(p.stop_loss_pct)))
        return [
            Intent(
                kind=IntentKind.ENTER, side=Side.BUY, qty=qty,
                stop_price=stop, reason="crash_momentum",
            )
        ]
