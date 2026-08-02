"""crash_momentum_v1 (폭락 익일 인버스) behavior tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from short_trading_bot.domain.enums import PositionState, Resolution
from short_trading_bot.domain.factory import PositionFactory
from short_trading_bot.domain.params import PositionParams
from short_trading_bot.domain.signal import IntentKind, Signal
from short_trading_bot.strategy.algorithms.crash_momentum import (
    CrashMomentum,
    CrashMomentumParams,
)
from short_trading_bot.strategy.base import StrategyContext
from short_trading_bot.strategy.templates import StrategyTemplate

from ._helpers import make_snapshot

PARAMS = PositionParams(strategy_id="crash_momentum_v1", resolution=Resolution.D1)


def _ctx(
    state: PositionState,
    close: float,
    *,
    prev_close: float | None = None,
    bars_held: int = 0,
    stop: float | None = None,
    equity: float = 10_000_000,
) -> StrategyContext:
    prev = (
        make_snapshot(prev_close, ticker="114800", resolution=Resolution.D1)
        if prev_close is not None
        else None
    )
    return StrategyContext(
        snapshot=make_snapshot(close, ticker="114800", resolution=Resolution.D1),
        state=state,
        qty=Decimal("100") if state is PositionState.HOLDING else Decimal(0),
        avg_entry=Decimal(str(close)),
        peak_price=Decimal(str(close)),
        bars_held=bars_held,
        params=PARAMS,
        equity=Decimal(str(equity)),
        initial_stop=Decimal(str(stop)) if stop is not None else None,
        original_qty=Decimal("100"),
        prev=prev,
    )


def test_enters_on_crash_day() -> None:
    """인버스 ETF +3% (지수 -3% 프록시) → 익일 진입, alloc_pct 사이징."""
    strat = CrashMomentum(CrashMomentumParams())
    intents = strat.evaluate(_ctx(PositionState.WATCHING, 10_300, prev_close=10_000))
    assert intents[0].kind is IntentKind.ENTER
    assert intents[0].stop_price is None  # 기본 = 검증된 손절 없는 보유
    # 1,000만 x 5% / 10,300 = 48주
    assert intents[0].qty == Decimal(48)


def test_holds_below_threshold() -> None:
    strat = CrashMomentum(CrashMomentumParams())
    intents = strat.evaluate(_ctx(PositionState.WATCHING, 10_270, prev_close=10_000))
    assert intents[0].kind is IntentKind.HOLD
    assert intents[0].reason == "no_crash"


def test_holds_without_prev_bar() -> None:
    strat = CrashMomentum(CrashMomentumParams())
    intents = strat.evaluate(_ctx(PositionState.WATCHING, 10_300))
    assert intents[0].kind is IntentKind.HOLD
    assert intents[0].reason == "no_prev"


def test_exits_after_hold_days() -> None:
    strat = CrashMomentum(CrashMomentumParams())
    held = strat.evaluate(_ctx(PositionState.HOLDING, 10_400, bars_held=2))
    assert held[0].kind is IntentKind.HOLD
    done = strat.evaluate(_ctx(PositionState.HOLDING, 10_400, bars_held=3))
    assert done[0].kind is IntentKind.EXIT
    assert done[0].reason == "hold_expiry"


def test_optional_stop_loss() -> None:
    """stop_loss_pct를 켜면 진입 시 스톱이 실리고, 이탈 시 hard_stop 청산."""
    strat = CrashMomentum(CrashMomentumParams(stop_loss_pct=0.10))
    enter = strat.evaluate(_ctx(PositionState.WATCHING, 10_300, prev_close=10_000))
    assert enter[0].kind is IntentKind.ENTER
    assert enter[0].stop_price == Decimal("10300") * Decimal("0.90")

    hit = strat.evaluate(_ctx(PositionState.HOLDING, 9_200, bars_held=1, stop=9_270))
    assert hit[0].kind is IntentKind.EXIT
    assert hit[0].reason == "hard_stop"


def test_consecutive_crash_while_holding_does_not_add() -> None:
    """보유 중 또 폭락해도 추가 매수 없이 보유만 (검증 시나리오 그대로)."""
    strat = CrashMomentum(CrashMomentumParams())
    intents = strat.evaluate(
        _ctx(PositionState.HOLDING, 10_900, prev_close=10_500, bars_held=1)
    )
    assert intents[0].kind is IntentKind.HOLD


def test_size_zero_when_price_exceeds_allocation() -> None:
    strat = CrashMomentum(CrashMomentumParams())
    intents = strat.evaluate(
        _ctx(PositionState.WATCHING, 10_300, prev_close=10_000, equity=100_000)
    )
    assert intents[0].kind is IntentKind.HOLD
    assert intents[0].reason == "size_zero"


def test_factory_rejects_intraday_resolution() -> None:
    """D1 전용 — 분봉 템플릿으로 랏을 만들려 하면 거부."""
    template = StrategyTemplate(strategy_id="crash_momentum_v1", resolution=Resolution.M5)
    with pytest.raises(ValueError, match="resolution"):
        PositionFactory().create(Signal(ticker="114800"), template)
