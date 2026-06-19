from short_trading_bot.strategy.rules import blocks

from ._helpers import make_snapshot


def test_regime_bullish_pass_and_fail() -> None:
    ok = make_snapshot(110, sma_20=105, sma_60=100, adx_14=30, plus_di=30, minus_di=10)
    assert blocks.regime_bullish(ok, adx_min=25)

    weak_adx = make_snapshot(110, sma_20=105, sma_60=100, adx_14=10, plus_di=30, minus_di=10)
    assert not blocks.regime_bullish(weak_adx, adx_min=25)


def test_regime_none_fails() -> None:
    assert not blocks.regime_bullish(make_snapshot(110), adx_min=25)


def test_regime_full_alignment() -> None:
    s = make_snapshot(110, sma_5=109, sma_20=105, sma_60=100, adx_14=30, plus_di=30, minus_di=10)
    assert blocks.regime_bullish(s, adx_min=25, require_full_alignment=True)
    bad = make_snapshot(110, sma_5=120, sma_20=105, sma_60=100, adx_14=30, plus_di=30, minus_di=10)
    assert not blocks.regime_bullish(bad, adx_min=25, require_full_alignment=True)


def test_macd_cross_up() -> None:
    prev = make_snapshot(100, macd=0.4, macd_signal=0.5)
    snap = make_snapshot(101, macd=1.0, macd_signal=0.5)
    assert blocks.macd_cross_up(snap, prev)
    assert not blocks.macd_cross_up(snap, None)


def test_golden_cross() -> None:
    prev = make_snapshot(100, sma_5=99, sma_20=100)
    snap = make_snapshot(101, sma_5=102, sma_20=100)
    assert blocks.golden_cross(snap, prev)


def test_breakout_high() -> None:
    prev = make_snapshot(100, high_20=108)
    assert blocks.breakout_high(make_snapshot(110), prev)
    assert not blocks.breakout_high(make_snapshot(107), prev)


def test_momentum_and_volume() -> None:
    s = make_snapshot(100, rsi_14=60, macd_hist=0.5, rvol=2.0)
    assert blocks.momentum_ok(s, rsi_low=50, rsi_high=70)
    assert blocks.volume_ok(s, rvol_min=1.5)
    weak = make_snapshot(100, rsi_14=80, macd_hist=-0.1, rvol=1.0)
    assert not blocks.momentum_ok(weak, rsi_low=50, rsi_high=70)
    assert not blocks.volume_ok(weak, rvol_min=1.5)


def test_exit_reasons() -> None:
    prev = make_snapshot(100, sma_5=10, sma_20=9, rsi_14=75)
    snap = make_snapshot(100, sma_20=120, sma_5=8, rsi_14=60)
    reasons = blocks.exit_reasons(snap, prev)
    assert "below_ma20" in reasons
    assert "dead_cross_5_20" in reasons
    assert "rsi_rolldown" in reasons
