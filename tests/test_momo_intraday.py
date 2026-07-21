"""momo_intraday_v1 (급등 모멘텀 합류 단타) behavior tests."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from short_trading_bot.domain.enums import PositionState, Resolution
from short_trading_bot.domain.params import PositionParams
from short_trading_bot.domain.signal import IntentKind
from short_trading_bot.strategy.algorithms.momo_intraday import KST, MomoIntraday, MomoParams
from short_trading_bot.strategy.base import StrategyContext

from ._helpers import make_snapshot

PARAMS = PositionParams(strategy_id="momo_intraday_v1", resolution=Resolution.M5)


def _ts(hh: int, mm: int) -> datetime:
    return datetime(2026, 7, 6, hh, mm, tzinfo=KST)  # Monday


def _ctx(
    state: PositionState,
    close: float,
    hh: int,
    mm: int,
    *,
    prev_close: float | None = None,
    qty: float = 0.0,
    avg: float = 0.0,
    stop: float | None = None,
    peak: float = 0.0,
    original: float = 0.0,
    news: float | None = None,
    **ind: float,
) -> StrategyContext:
    return StrategyContext(
        snapshot=make_snapshot(close, resolution=Resolution.M5, ts=_ts(hh, mm), bar_count=100, **ind),
        state=state,
        qty=Decimal(str(qty)),
        avg_entry=Decimal(str(avg)),
        peak_price=Decimal(str(peak)),
        bars_held=0,
        params=PARAMS,
        equity=Decimal("10000000"),
        initial_stop=Decimal(str(stop)) if stop is not None else None,
        original_qty=Decimal(str(original)),
        news_ewma=news,
        prev=make_snapshot(prev_close, resolution=Resolution.M5) if prev_close is not None else None,
    )


GOOD = {"rvol": 3.0, "vwap": 9800.0, "atr_14": 100.0}


def test_joins_rising_momentum_with_atr_stop() -> None:
    strat = MomoIntraday(MomoParams())
    out = strat.evaluate(_ctx(PositionState.WATCHING, 10000, 10, 30, prev_close=9900, **GOOD))
    intent = out[0]
    assert intent.kind is IntentKind.ENTER and intent.reason == "momo_join"
    assert intent.qty is not None and intent.qty > 0
    assert intent.stop_price == Decimal("9850.0")  # 10000 - 1.5*100


def test_entry_blocked_by_filters() -> None:
    strat = MomoIntraday(MomoParams())
    cases = {
        "rvol_low": dict(GOOD, rvol=1.0),
        "below_vwap": dict(GOOD, vwap=10100.0),
        "no_atr": {"rvol": 3.0, "vwap": 9800.0},
    }
    for reason, ind in cases.items():
        out = strat.evaluate(_ctx(PositionState.WATCHING, 10000, 10, 30, prev_close=9900, **ind))
        assert out[0].kind is IntentKind.HOLD and out[0].reason == reason

    # 직전 봉보다 내려온 봉에서는 합류하지 않는다 (하락 전환에 물리기 방지)
    out = strat.evaluate(_ctx(PositionState.WATCHING, 10000, 10, 30, prev_close=10100, **GOOD))
    assert out[0].reason == "not_rising"


def test_entry_cutoff_and_daily_limit() -> None:
    strat = MomoIntraday(MomoParams())
    out = strat.evaluate(_ctx(PositionState.WATCHING, 10000, 14, 30, prev_close=9900, **GOOD))
    assert out[0].reason == "entry_cutoff"

    assert strat.evaluate(_ctx(PositionState.WATCHING, 10000, 10, 30, prev_close=9900, **GOOD))[0].kind is IntentKind.ENTER
    out = strat.evaluate(_ctx(PositionState.WATCHING, 10100, 10, 35, prev_close=10000, **GOOD))
    assert out[0].reason == "daily_entry_limit"


def test_exits_in_priority_order() -> None:
    strat = MomoIntraday(MomoParams())
    held = dict(qty=10, avg=10000, stop=9850, peak=10200, original=10)

    # 장마감 강제청산이 최우선
    out = strat.evaluate(_ctx(PositionState.HOLDING, 10500, 15, 10, **held, **GOOD))
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "session_end"

    # 하드 스톱
    out = strat.evaluate(_ctx(PositionState.HOLDING, 9840, 11, 0, **held, **GOOD))
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "hard_stop"

    # VWAP 이탈 = 모멘텀 소멸
    out = strat.evaluate(
        _ctx(PositionState.HOLDING, 9900, 11, 0, **held, rvol=3.0, vwap=9950.0, atr_14=100.0)
    )
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "vwap_lost"


def test_take_profit_rung() -> None:
    strat = MomoIntraday(MomoParams())
    # risk = 150; TP1 기본 r_multiple 도달 (avg 10000, stop 9850 → 1R = 10150)
    out = strat.evaluate(
        _ctx(
            PositionState.HOLDING, 10500, 11, 0,
            qty=10, avg=10000, stop=9850, peak=10500, original=10,
            rvol=3.0, vwap=10100.0, atr_14=100.0,
        )
    )
    assert out[0].kind is IntentKind.TRIM and out[0].reason == "take_profit_1"


def test_registered_in_registry() -> None:
    from short_trading_bot.strategy.registry import create_strategy

    strat = create_strategy("momo_intraday_v1", {"min_rvol": 2.5})
    assert isinstance(strat, MomoIntraday)


def test_vwap_exit_buffer_suppresses_shallow_dip() -> None:
    """버퍼 설정 시 VWAP를 살짝 밑돈 정도로는 청산하지 않는다."""
    strat = MomoIntraday(MomoParams(vwap_exit_buffer_pct=0.01))
    held = dict(qty=10, avg=10000, stop=9700, peak=10050, original=10)
    out = strat.evaluate(
        _ctx(PositionState.HOLDING, 9900, 11, 0, **held, rvol=3.0, vwap=9950.0, atr_14=200.0)
    )
    assert out[0].kind is IntentKind.HOLD  # 9900 > 9950*0.99=9850.5 → 유지

    out = strat.evaluate(
        _ctx(PositionState.HOLDING, 9800, 11, 0, **held, rvol=3.0, vwap=9950.0, atr_14=200.0)
    )
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "vwap_lost"  # 버퍼 초과 이탈


def test_min_rising_bars_requires_streak() -> None:
    """min_rising_bars=2: 연속 상승봉 2개가 쌓여야 진입."""
    strat = MomoIntraday(MomoParams(min_rising_bars=2))
    # 첫 상승봉 (streak 1) — 아직 부족
    out = strat.evaluate(_ctx(PositionState.WATCHING, 10000, 10, 30, prev_close=9900, **GOOD))
    assert out[0].kind is IntentKind.HOLD and out[0].reason == "not_rising_streak"
    # 두 번째 상승봉 (streak 2) — 진입
    out = strat.evaluate(_ctx(PositionState.WATCHING, 10100, 10, 31, prev_close=10000, **GOOD))
    assert out[0].kind is IntentKind.ENTER


def test_require_breakout_blocks_inside_bar() -> None:
    """require_breakout: 직전 봉 고가를 넘지 못하면 합류하지 않는다."""
    from short_trading_bot.domain.enums import Resolution as _R

    from ._helpers import make_snapshot as _snap

    strat = MomoIntraday(MomoParams(require_breakout=True))
    ctx = _ctx(PositionState.WATCHING, 10000, 10, 30, prev_close=9900, **GOOD)
    ctx = StrategyContext(
        snapshot=ctx.snapshot, state=ctx.state, qty=ctx.qty, avg_entry=ctx.avg_entry,
        peak_price=ctx.peak_price, bars_held=ctx.bars_held, params=ctx.params,
        equity=ctx.equity, initial_stop=ctx.initial_stop, original_qty=ctx.original_qty,
        news_ewma=ctx.news_ewma,
        prev=_snap(9900, resolution=_R.M5, high=10050.0),  # 고가 10050 > 종가 10000
    )
    out = strat.evaluate(ctx)
    assert out[0].kind is IntentKind.HOLD and out[0].reason == "no_breakout"


def test_vwap_exit_confirm_bars_waits_for_streak() -> None:
    """vwap_exit_confirm_bars=2: 이탈 봉 1개로는 유지, 연속 2개면 청산."""
    strat = MomoIntraday(MomoParams(vwap_exit_confirm_bars=2))
    held = dict(qty=10, avg=10000, stop=9600, peak=10050, original=10)
    out = strat.evaluate(
        _ctx(PositionState.HOLDING, 9900, 11, 0, **held, rvol=3.0, vwap=9950.0, atr_14=300.0)
    )
    assert out[0].kind is IntentKind.HOLD  # 1봉째 이탈 — 대기
    out = strat.evaluate(
        _ctx(PositionState.HOLDING, 9900, 11, 1, **held, rvol=3.0, vwap=9950.0, atr_14=300.0)
    )
    assert out[0].kind is IntentKind.EXIT and out[0].reason == "vwap_lost"  # 2봉 연속
    # 회복하면 카운터 리셋
    strat2 = MomoIntraday(MomoParams(vwap_exit_confirm_bars=2))
    strat2.evaluate(_ctx(PositionState.HOLDING, 9900, 11, 0, **held, rvol=3.0, vwap=9950.0, atr_14=300.0))
    strat2.evaluate(_ctx(PositionState.HOLDING, 10000, 11, 1, **held, rvol=3.0, vwap=9950.0, atr_14=300.0))
    out = strat2.evaluate(_ctx(PositionState.HOLDING, 9900, 11, 2, **held, rvol=3.0, vwap=9950.0, atr_14=300.0))
    assert out[0].kind is IntentKind.HOLD  # 리셋 후 다시 1봉째
