from decimal import Decimal

import pytest
from pydantic import ValidationError

import short_trading_bot.strategy.algorithms  # noqa: F401  (register plugins)
from short_trading_bot.domain.params import PositionParams
from short_trading_bot.strategy.algorithms.trend_long import TrendLongV1
from short_trading_bot.strategy.registry import (
    all_strategies,
    create_strategy,
    get_strategy_cls,
    register_strategy,
)
from short_trading_bot.strategy.sizing import (
    atr_stop,
    chandelier_stop,
    pct_stop,
    risk_based_qty,
)

# --- PositionParams (frozen) ---

def test_params_frozen() -> None:
    p = PositionParams(strategy_id="trend_long_v1")
    with pytest.raises(ValidationError):
        p.risk_per_trade = 0.5  # type: ignore[misc]


def test_params_serialize() -> None:
    d = PositionParams(strategy_id="trend_long_v1").model_dump()
    assert d["strategy_id"] == "trend_long_v1"
    assert len(d["take_profit"]) == 2


# --- registry ---

def test_strategy_registered() -> None:
    assert "trend_long_v1" in all_strategies()
    assert get_strategy_cls("trend_long_v1") is TrendLongV1


def test_create_strategy_validates_params() -> None:
    s = create_strategy("trend_long_v1", {"adx_min": 30})
    assert isinstance(s, TrendLongV1)
    assert s._p.adx_min == 30.0


def test_unknown_strategy_raises() -> None:
    with pytest.raises(KeyError):
        get_strategy_cls("does_not_exist")


def test_duplicate_registration_raises() -> None:
    with pytest.raises(ValueError):

        @register_strategy("trend_long_v1")
        class _Dup(TrendLongV1):
            pass


# --- sizing ---

def test_risk_based_qty() -> None:
    qty = risk_based_qty(Decimal("10000000"), 0.01, Decimal("110"), Decimal("106"))
    assert qty == Decimal("25000")  # 100000 budget / 4 per-share risk


def test_risk_based_qty_invalid() -> None:
    assert risk_based_qty(Decimal("100"), 0.01, Decimal("100"), Decimal("100")) == Decimal("0")


def test_risk_based_qty_capped_by_equity() -> None:
    # 타이트한 손절(0.1% 거리) → 리스크식 수량(2000주)이 계좌 초과 → 자본 95% 캡(95주)
    qty = risk_based_qty(Decimal("10000000"), 0.02, Decimal("100000"), Decimal("99900"))
    assert qty == Decimal("95")
    assert qty * Decimal("100000") <= Decimal("10000000") * Decimal("0.95")


def test_stops() -> None:
    assert atr_stop(Decimal("110"), 2.0, 2.0) == Decimal("106.0")
    assert chandelier_stop(Decimal("120"), 2.0, 3.0) == Decimal("114.0")
    assert pct_stop(Decimal("100"), 0.05) == Decimal("95.00")


def test_risk_based_qty_cost_buffer_shrinks_size() -> None:
    """비용 버퍼: 주당 리스크에 진입가x버퍼를 얹어 수량이 줄어든다 (0 = 기존 동작)."""
    from decimal import Decimal

    from short_trading_bot.strategy.sizing import risk_based_qty

    eq, entry, stop = Decimal("10000000"), Decimal("10000"), Decimal("9800")
    base = risk_based_qty(eq, 0.005, entry, stop)
    assert base == Decimal("250")  # 50,000 / 200원
    buffered = risk_based_qty(eq, 0.005, entry, stop, cost_buffer_pct=0.0033)
    assert buffered == Decimal("214")  # 50,000 / (200 + 33)
    assert risk_based_qty(eq, 0.005, entry, stop, cost_buffer_pct=0.0) == base
