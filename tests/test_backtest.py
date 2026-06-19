from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from short_trading_bot.backtest.costs import CostModel
from short_trading_bot.backtest.harness import Backtester
from short_trading_bot.backtest.metrics import compute_metrics
from short_trading_bot.domain.enums import Market, Resolution
from short_trading_bot.market.types import Bar
from short_trading_bot.strategy.templates import StrategyTemplate

# --- CostModel ---

def test_cost_model() -> None:
    cm = CostModel(fee_bps=1.5, sell_tax_bps=18.0, slippage_bps=5.0)
    assert cm.buy_price(Decimal("100")) == Decimal("100.05")  # +5 bps
    assert cm.sell_price(Decimal("100")) == Decimal("99.95")  # -5 bps
    assert cm.fee(Decimal("1000000")) == Decimal("150.00000")
    assert cm.sell_tax(Decimal("1000000"), Market.KRX) == Decimal("1800.000")
    assert cm.sell_tax(Decimal("1000000"), Market.NASD) == Decimal("0")  # no KR tax overseas


# --- metrics ---

def test_metrics_known_values() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    curve = [
        (base, Decimal("1000")),
        (base + timedelta(days=1), Decimal("1100")),
        (base + timedelta(days=2), Decimal("990")),
        (base + timedelta(days=3), Decimal("1050")),
    ]
    m = compute_metrics(
        Decimal("1000"),
        curve,
        trade_pnls=[Decimal("50"), Decimal("-20"), Decimal("30")],
        trade_rs=[2.0, -1.0],
    )
    assert m.num_trades == 3
    assert m.win_rate == pytest.approx(2 / 3)
    assert m.profit_factor == pytest.approx(4.0)  # 80 / 20
    assert m.avg_r == pytest.approx(0.5)
    assert m.total_return_pct == pytest.approx(5.0)
    assert m.max_drawdown_pct == pytest.approx(10.0)  # 1100 -> 990


# --- end-to-end backtest through the live code path ---

def _bar(close: float, ts: datetime, *, high: float | None = None, low: float | None = None) -> Bar:
    c = Decimal(str(close))
    return Bar(
        ticker="005930",
        resolution=Resolution.D1,
        ts=ts,
        open=c,
        high=Decimal(str(high if high is not None else close)),
        low=Decimal(str(low if low is not None else close)),
        close=c,
        volume=Decimal("1000"),
        value=c * Decimal("1000"),
    )


def _uptrend_then_down() -> list[Bar]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bars: list[Bar] = []
    day = 0
    for i in range(70):  # steady uptrend (new highs -> breakout trigger), past 60-bar warmup
        c = 100.0 + 2 * i
        bars.append(_bar(c, base + timedelta(days=day), high=c, low=c - 1))
        day += 1
    top = 100.0 + 2 * 69
    for j in range(1, 26):  # sharp downtrend -> MA break / stop -> exit
        c = top - 5 * j
        bars.append(_bar(c, base + timedelta(days=day), high=c + 1, low=c))
        day += 1
    return bars


def test_backtest_produces_a_round_trip() -> None:
    template = StrategyTemplate(
        strategy_id="trend_long_v1",
        resolution=Resolution.D1,
        strategy_params={"require_confirm": False},  # gate+trigger entry (no volume/momentum)
    )
    result = Backtester(template, Decimal("10000000")).run(_uptrend_then_down())

    assert len(result.equity_curve) == 95
    assert len(result.trades) >= 1
    assert result.metrics.num_trades == len(result.trades)
    assert isinstance(result.final_equity, Decimal)
    trade = result.trades[0]
    assert trade.ticker == "005930"
    assert trade.qty > 0
    assert trade.entry_price > 0 and trade.exit_price > 0
