"""time_window_v1 (시간창 단타) behavior tests."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from short_trading_bot.domain.enums import PositionState, Resolution
from short_trading_bot.domain.params import PositionParams
from short_trading_bot.domain.signal import IntentKind
from short_trading_bot.strategy.algorithms.time_window import KST, TimeWindow, TimeWindowParams
from short_trading_bot.strategy.base import StrategyContext

from ._helpers import make_snapshot

PARAMS = PositionParams(strategy_id="time_window_v1", resolution=Resolution.M1)


def _ctx(state: PositionState, close: float, hh: int, mm: int, *,
         day: int = 6, qty: float = 0.0, stop: float | None = None,
         **ind: float) -> StrategyContext:
    # 2026-07-06 = 월요일; day 인자로 요일 제어
    return StrategyContext(
        snapshot=make_snapshot(close, resolution=Resolution.M1,
                               ts=datetime(2026, 7, day, hh, mm, tzinfo=KST), bar_count=100, **ind),
        state=state,
        qty=Decimal(str(qty)),
        avg_entry=Decimal("10000"),
        peak_price=Decimal(str(close)),
        bars_held=0,
        params=PARAMS,
        equity=Decimal("10000000"),
        initial_stop=Decimal(str(stop)) if stop is not None else None,
        original_qty=Decimal(str(qty)),
        news_ewma=None,
        prev=None,
    )


def _warm(strat: TimeWindow, open_price: float) -> None:
    """09:00 봉으로 세션 시가를 세팅."""
    strat.evaluate(_ctx(PositionState.WATCHING, open_price, 9, 0, atr_14=50.0))


def test_enters_on_morning_down_day_in_window() -> None:
    strat = TimeWindow(TimeWindowParams())
    _warm(strat, 10000)  # 세션 시가 10000
    out = strat.evaluate(_ctx(PositionState.WATCHING, 9950, 14, 5, atr_14=50.0))  # -0.5%
    assert out[0].kind is IntentKind.ENTER and out[0].reason == "time_window"
    assert out[0].stop_price == Decimal("9825.0")  # 9950 - 2.5*50


def test_blocked_outside_window_or_wrong_day_or_morning_up() -> None:
    strat = TimeWindow(TimeWindowParams())
    _warm(strat, 10000)
    # 오전 상승일 → morning_filter
    out = strat.evaluate(_ctx(PositionState.WATCHING, 10100, 14, 5, atr_14=50.0))
    assert out[0].reason == "morning_filter"
    # 시간창 밖 (13:00)
    out = strat.evaluate(_ctx(PositionState.WATCHING, 9950, 13, 0, atr_14=50.0))
    assert out[0].reason == "outside_window"
    # 요일 마스크 (화요일만 허용인데 월요일)
    strat2 = TimeWindow(TimeWindowParams(weekdays=[1]))
    _warm(strat2, 10000)
    out = strat2.evaluate(_ctx(PositionState.WATCHING, 9950, 14, 5, atr_14=50.0))
    assert out[0].reason == "weekday_off"


def test_one_entry_per_day_and_flat_by_close() -> None:
    strat = TimeWindow(TimeWindowParams())
    _warm(strat, 10000)
    assert strat.evaluate(_ctx(PositionState.WATCHING, 9950, 14, 5, atr_14=50.0))[0].kind is IntentKind.ENTER
    # 같은 날 두 번째 진입 시도 → 차단
    out = strat.evaluate(_ctx(PositionState.WATCHING, 9940, 14, 6, atr_14=50.0))
    assert out[0].reason == "daily_entry_limit"
    # 보유 중 15:20 → 무조건 청산
    out = strat.evaluate(_ctx(PositionState.HOLDING, 9990, 15, 20, qty=10, stop=9825, atr_14=50.0))
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "session_end"
    # 보유 중 손절 터치
    out = strat.evaluate(_ctx(PositionState.HOLDING, 9820, 14, 30, qty=10, stop=9825, atr_14=50.0))
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "hard_stop"


def test_unconditional_mode_and_registry() -> None:
    from short_trading_bot.strategy.registry import create_strategy

    strat = create_strategy("time_window_v1", {"morning_return_max": None})
    assert isinstance(strat, TimeWindow)
    _warm(strat, 10000)
    # 무조건부 모드: 오전 상승일에도 진입
    out = strat.evaluate(_ctx(PositionState.WATCHING, 10100, 14, 5, atr_14=50.0))
    assert out[0].kind is IntentKind.ENTER
