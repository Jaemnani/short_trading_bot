from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from short_trading_bot.app.campaign_manager import CampaignManager
from short_trading_bot.domain.campaign import Campaign, IllegalCampaignTransition
from short_trading_bot.domain.enums import CampaignState, Resolution
from short_trading_bot.market.types import Bar
from short_trading_bot.strategy.templates import StrategyTemplate


def _bar(close: float, ts: datetime) -> Bar:
    c = Decimal(str(close))
    return Bar(
        ticker="005930", resolution=Resolution.D1, ts=ts,
        open=c, high=c, low=c - 1, close=c, volume=Decimal("1000"), value=c * Decimal("1000"),
    )


def _uptrend_tape() -> list[Bar]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # pure uptrend: position enters after warmup and never exits -> forced liquidation at end
    return [_bar(100.0 + 2 * i, base + timedelta(days=i)) for i in range(80)]


def _campaign(tape: list[Bar]) -> Campaign:
    return Campaign(
        campaign_id="camp-1",
        name="Q1 momentum basket",
        start_at=tape[0].ts,
        end_at=tape[-1].ts,
        initial_budget=Decimal("10000000"),
        template=StrategyTemplate(
            strategy_id="trend_long_v1",
            resolution=Resolution.D1,
            strategy_params={"require_confirm": False},
        ),
        tickers=["005930"],
    )


# --- state machine ---

def test_campaign_transitions() -> None:
    c = _campaign(_uptrend_tape())
    assert c.status is CampaignState.SCHEDULED
    c.transition_to(CampaignState.RUNNING)
    c.transition_to(CampaignState.LIQUIDATING)
    c.transition_to(CampaignState.SETTLED)
    assert c.status is CampaignState.SETTLED


def test_illegal_campaign_transition() -> None:
    c = _campaign(_uptrend_tape())
    with pytest.raises(IllegalCampaignTransition):
        c.transition_to(CampaignState.SETTLED)  # cannot skip RUNNING/LIQUIDATING


# --- run lifecycle + settlement invariant ---

def test_campaign_run_settles_and_reconciles() -> None:
    tape = _uptrend_tape()
    campaign = _campaign(tape)
    report = CampaignManager().run(campaign, tape)

    assert campaign.status is CampaignState.SETTLED
    assert report.num_trades >= 1
    # the headline guarantee: net P&L == end_cash - initial_budget == Σ trade P&L
    assert report.realized_pnl == report.end_cash - Decimal("10000000")
    assert report.reconciles
    # everything was force-liquidated at period end (no open position left)
    assert any(t.exit_reason == "campaign_end" for t in report.trades)
    assert report.return_pct == pytest.approx(
        float(report.realized_pnl / Decimal("10000000") * 100)
    )


def test_campaign_tickers_outside_basket_ignored() -> None:
    tape = _uptrend_tape()
    # inject bars for a ticker NOT in the basket; they must not create trades
    other = [
        Bar(
            ticker="000660", resolution=Resolution.D1, ts=b.ts,
            open=b.close, high=b.close, low=b.close, close=b.close, volume=Decimal("1000"),
            value=b.close * Decimal("1000"),
        )
        for b in tape
    ]
    report = CampaignManager().run(_campaign(tape), tape + other)
    assert all(t.ticker == "005930" for t in report.trades)
