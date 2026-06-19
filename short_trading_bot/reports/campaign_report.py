"""Campaign settlement report.

The headline guarantee: ``realized_pnl == end_cash - initial_budget``, and because the
campaign starts and ends 100% in cash, this also equals Σ(per-trade net P&L). The
``reconciles`` flag asserts that identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..backtest.harness import BacktestResult, BTTrade
from ..backtest.metrics import BacktestMetrics
from ..domain.campaign import Campaign

_EPS = Decimal("0.01")


@dataclass(slots=True)
class CampaignReport:
    campaign_id: str
    name: str
    initial_budget: Decimal
    end_cash: Decimal
    realized_pnl: Decimal  # end_cash - initial_budget
    return_pct: float
    total_fees: Decimal
    total_tax: Decimal
    num_trades: int
    trades: list[BTTrade] = field(default_factory=list)
    metrics: BacktestMetrics | None = None

    @property
    def trade_pnl_sum(self) -> Decimal:
        return sum((t.net_pnl for t in self.trades), Decimal(0))

    @property
    def reconciles(self) -> bool:
        """True when realized_pnl matches the sum of trade P&Ls (capital fully cycled)."""
        return abs(self.realized_pnl - self.trade_pnl_sum) <= _EPS


def build_campaign_report(campaign: Campaign, result: BacktestResult) -> CampaignReport:
    end_cash = result.final_equity  # all lots liquidated -> equity is pure cash
    realized = end_cash - campaign.initial_budget
    return_pct = (
        float(realized / campaign.initial_budget * 100) if campaign.initial_budget > 0 else 0.0
    )
    return CampaignReport(
        campaign_id=campaign.campaign_id,
        name=campaign.name,
        initial_budget=campaign.initial_budget,
        end_cash=end_cash,
        realized_pnl=realized,
        return_pct=return_pct,
        total_fees=sum((t.fees for t in result.trades), Decimal(0)),
        total_tax=sum((t.tax for t in result.trades), Decimal(0)),
        num_trades=len(result.trades),
        trades=result.trades,
        metrics=result.metrics,
    )
