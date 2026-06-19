"""CampaignManager — orchestrates a campaign's lifecycle.

P8 runs the campaign in backtest mode over a historical tape: deploy from the start
budget, let lots trade within that capital envelope over [start_at, end_at], force-
liquidate all survivors at the end, then settle. The live engine reuses the same
Campaign/report types and drives start/stop from the scheduler (P9+).
"""

from __future__ import annotations

from ..backtest.costs import CostModel
from ..backtest.harness import Backtester
from ..domain.campaign import Campaign
from ..domain.enums import CampaignState
from ..market.types import Bar
from ..reports.campaign_report import CampaignReport, build_campaign_report


class CampaignManager:
    def __init__(self, cost_model: CostModel | None = None) -> None:
        self._cost = cost_model or CostModel()

    def run(self, campaign: Campaign, tape: list[Bar]) -> CampaignReport:
        campaign.transition_to(CampaignState.RUNNING)

        basket = set(campaign.tickers)
        scoped = [
            bar
            for bar in tape
            if bar.ticker in basket and campaign.start_at <= bar.ts <= campaign.end_at
        ]

        # Capital isolation: starting equity IS the campaign budget; the harness's
        # buying-power check confines all trading to it. liquidate_open_at_end forces a
        # full cash-out so the settlement P&L is exact.
        result = Backtester(
            campaign.template,
            campaign.initial_budget,
            self._cost,
            liquidate_open_at_end=True,
        ).run(scoped)

        campaign.transition_to(CampaignState.LIQUIDATING)
        campaign.transition_to(CampaignState.SETTLED)
        return build_campaign_report(campaign, result)
