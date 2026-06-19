"""Campaign — a period-bounded, capital-isolated trading session.

Set a period + start budget; lots trade autonomously within that budget; at end the
campaign force-liquidates everything to cash so net P&L = end_cash - initial_budget is
exact and unambiguous. (P8 runs campaigns in backtest mode; the live engine wires the
start/stop to the scheduler in P9+.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..strategy.templates import StrategyTemplate
from .enums import CampaignState, Currency

_ALLOWED: dict[CampaignState, frozenset[CampaignState]] = {
    CampaignState.SCHEDULED: frozenset({CampaignState.RUNNING}),
    CampaignState.RUNNING: frozenset({CampaignState.LIQUIDATING}),
    CampaignState.LIQUIDATING: frozenset({CampaignState.SETTLED}),
    CampaignState.SETTLED: frozenset(),
}


class IllegalCampaignTransition(RuntimeError):
    pass


@dataclass
class Campaign:
    campaign_id: str
    name: str
    start_at: datetime
    end_at: datetime
    initial_budget: Decimal
    template: StrategyTemplate  # one strategy applied to the basket (P8 simplification)
    tickers: list[str] = field(default_factory=list)
    currency: Currency = Currency.KRW
    status: CampaignState = CampaignState.SCHEDULED

    def transition_to(self, new_state: CampaignState) -> None:
        if new_state not in _ALLOWED[self.status]:
            raise IllegalCampaignTransition(f"{self.status} -> {new_state} not allowed")
        self.status = new_state
