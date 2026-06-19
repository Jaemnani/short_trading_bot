import numpy as np
import pytest

from short_trading_bot.market import indicators as ind


def test_sma_last() -> None:
    assert ind.sma_last(np.array([1.0, 2, 3, 4, 5]), 5) == 3.0
    assert ind.sma_last(np.array([1.0, 2]), 5) is None


def test_ema_last_constant() -> None:
    assert ind.ema_last(np.full(30, 5.0), 12) == pytest.approx(5.0)


def test_rsi_extremes() -> None:
    assert ind.rsi_wilder(np.arange(1, 21, dtype=float), 14) == pytest.approx(100.0)
    assert ind.rsi_wilder(np.arange(20, 0, -1, dtype=float), 14) == pytest.approx(0.0)
    assert ind.rsi_wilder(np.full(20, 7.0), 14) == 50.0  # flat -> neutral


def test_macd_constant_is_zero() -> None:
    macd, signal, hist = ind.macd(np.full(40, 10.0))
    assert macd == pytest.approx(0.0)
    assert signal == pytest.approx(0.0)
    assert hist == pytest.approx(0.0)


def test_bollinger_constant() -> None:
    mid, upper, lower, pctb, bw = ind.bollinger(np.full(25, 10.0), 20, 2.0)
    assert mid == pytest.approx(10.0)
    assert upper == pytest.approx(10.0) and lower == pytest.approx(10.0)
    assert pctb is None  # zero-width band
    assert bw == pytest.approx(0.0)


def test_atr_constant_is_zero() -> None:
    c = np.full(20, 50.0)
    assert ind.atr_wilder(c, c, c, 14) == pytest.approx(0.0)


def test_adx_uptrend_plus_di_dominates() -> None:
    close = np.arange(1, 41, dtype=float)
    high = close + 1
    low = close - 1
    adx, plus_di, minus_di = ind.adx_dmi(high, low, close, 14)
    assert plus_di is not None and minus_di is not None
    assert plus_di > minus_di
    assert minus_di == pytest.approx(0.0)
    assert adx == pytest.approx(100.0)


def test_stochastic_at_high() -> None:
    close = np.arange(1, 31, dtype=float)  # last close is the window max
    stoch_k, stoch_d = ind.stochastic(close, close, close, 14, 3, 3)
    assert stoch_k == pytest.approx(100.0)
    assert stoch_d == pytest.approx(100.0)


def test_obv_uptrend() -> None:
    close = np.array([1.0, 2, 3, 4, 5])
    vol = np.array([1.0, 1, 1, 1, 1])
    assert ind.obv(close, vol) == pytest.approx(4.0)


def test_vwap_equal_volume() -> None:
    typical = np.array([10.0, 20.0, 30.0])
    vol = np.array([1.0, 1.0, 1.0])
    assert ind.vwap(typical, vol) == pytest.approx(20.0)


def test_rvol() -> None:
    vol = np.array([1.0] * 20 + [3.0])
    assert ind.rvol(vol, 20) == pytest.approx(3.0)
    assert ind.rvol(np.ones(5), 20) is None
