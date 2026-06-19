"""Backtest harness — replays a historical tape through the SAME domain code path.

Indicators (IndicatorEngine) and strategy logic (PositionLot.evaluate / Strategy) are the
exact ones used live; only execution is simulated here (synchronous fills + CostModel)
instead of the async broker. One open lot per ticker at a time; on close, a fresh WATCHING
lot is armed so multiple round-trips can occur.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..domain.enums import PositionState, Side
from ..domain.factory import PositionFactory
from ..domain.position import PositionLot
from ..domain.signal import Intent, IntentKind, Signal
from ..market.indicators import IndicatorEngine
from ..market.types import Bar, IndicatorSnapshot
from ..strategy.templates import StrategyTemplate
from .costs import CostModel
from .metrics import BacktestMetrics, compute_metrics


@dataclass(slots=True)
class BTTrade:
    ticker: str
    market: str
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    gross_pnl: Decimal
    fees: Decimal
    tax: Decimal
    net_pnl: Decimal
    r_multiple: float | None
    opened_at: datetime
    closed_at: datetime
    exit_reason: str


@dataclass(slots=True)
class BacktestResult:
    starting_equity: Decimal
    final_equity: Decimal
    trades: list[BTTrade]
    equity_curve: list[tuple[datetime, Decimal]]
    metrics: BacktestMetrics


@dataclass
class _LotTrack:
    buy_qty: Decimal = Decimal(0)
    buy_notional: Decimal = Decimal(0)
    buy_fee: Decimal = Decimal(0)
    sell_qty: Decimal = Decimal(0)
    sell_notional: Decimal = Decimal(0)
    sell_fee: Decimal = Decimal(0)
    sell_tax: Decimal = Decimal(0)
    entry_ts: datetime | None = None
    entry_stop: Decimal | None = None
    exit_reason: str = "closed"


class Backtester:
    def __init__(
        self,
        template: StrategyTemplate,
        starting_equity: Decimal | str | int,
        cost_model: CostModel | None = None,
        *,
        news_ewma: float | None = None,
        liquidate_open_at_end: bool = False,
    ) -> None:
        self._template = template
        self._start = Decimal(str(starting_equity))
        self._cost = cost_model or CostModel()
        self._news = news_ewma
        self._liquidate_at_end = liquidate_open_at_end

    def run(self, tape: list[Bar]) -> BacktestResult:
        cash = self._start
        engine = IndicatorEngine()
        lots: dict[str, PositionLot] = {}
        tracks: dict[str, _LotTrack] = {}
        prev: dict[str, IndicatorSnapshot] = {}
        last_price: dict[str, Decimal] = {}
        trades: list[BTTrade] = []
        curve: list[tuple[datetime, Decimal]] = []
        counter = 0

        for bar in tape:
            ticker = bar.ticker
            last_price[ticker] = bar.close
            snap = engine.update(bar)

            lot = lots.get(ticker)
            if lot is not None and lot.is_open:
                lot.on_bar(bar.high)
            if lot is None or lot.is_terminal:
                counter += 1
                lot = PositionFactory.create(
                    Signal(ticker=ticker, market=self._template.market),
                    self._template,
                    lot_id=f"{ticker}-{counter}",
                )
                lots[ticker] = lot
                tracks[ticker] = _LotTrack()

            equity = self._equity(cash, lots, last_price)
            for intent in lot.evaluate(snap, equity, prev=prev.get(ticker), news_ewma=self._news):
                if intent.is_actionable:
                    cash = self._execute(intent, lot, tracks[ticker], bar, cash, trades)
            prev[ticker] = snap
            curve.append((bar.ts, self._equity(cash, lots, last_price)))

        if self._liquidate_at_end and tape:
            final_ts = tape[-1].ts
            exit_intent = Intent(kind=IntentKind.EXIT, side=Side.SELL, reason="campaign_end")
            for ticker, lot in lots.items():
                if lot.is_open:
                    price = last_price.get(ticker, lot.avg_entry)
                    close_bar = Bar(
                        ticker=ticker,
                        resolution=lot.params.resolution,
                        ts=final_ts,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        volume=Decimal(0),
                        value=Decimal(0),
                    )
                    cash = self._execute(exit_intent, lot, tracks[ticker], close_bar, cash, trades)
            curve.append((final_ts, self._equity(cash, lots, last_price)))

        metrics = compute_metrics(
            self._start,
            curve,
            [t.net_pnl for t in trades],
            [t.r_multiple for t in trades if t.r_multiple is not None],
        )
        return BacktestResult(
            starting_equity=self._start,
            final_equity=curve[-1][1] if curve else self._start,
            trades=trades,
            equity_curve=curve,
            metrics=metrics,
        )

    # -- internals -------------------------------------------------------

    def _equity(self, cash: Decimal, lots: dict[str, PositionLot], last: dict[str, Decimal]) -> Decimal:
        total = cash
        for ticker, lot in lots.items():
            if lot.qty > 0:
                total += lot.qty * last.get(ticker, lot.avg_entry)
        return total

    def _execute(
        self,
        intent: Intent,
        lot: PositionLot,
        track: _LotTrack,
        bar: Bar,
        cash: Decimal,
        trades: list[BTTrade],
    ) -> Decimal:
        if intent.side is Side.BUY:
            qty = intent.qty or Decimal(0)
            if qty <= 0:
                return cash
            price = self._cost.buy_price(bar.close)
            notional = price * qty
            fee = self._cost.fee(notional)
            if cash < notional + fee:
                return cash  # insufficient buying power
            cash -= notional + fee
            if track.entry_ts is None:
                track.entry_ts = bar.ts
            track.buy_qty += qty
            track.buy_notional += notional
            track.buy_fee += fee
            lot.apply_fill(Side.BUY, qty, price, is_add=intent.kind is IntentKind.ADD)
            if track.entry_stop is None and lot.initial_stop is not None:
                track.entry_stop = lot.initial_stop
            return cash

        # SELL: EXIT closes ALL remaining; TRIM uses its absolute qty (or fraction of original).
        if intent.kind is IntentKind.EXIT:
            qty = lot.qty
        elif intent.qty is not None:
            qty = intent.qty
        else:
            qty = Decimal(str(intent.fraction or 0)) * track.buy_qty
        qty = min(qty, lot.qty)
        if qty <= 0:
            return cash
        price = self._cost.sell_price(bar.close)
        notional = price * qty
        fee = self._cost.fee(notional)
        tax = self._cost.sell_tax(notional, lot.market)
        cash += notional - fee - tax
        track.sell_qty += qty
        track.sell_notional += notional
        track.sell_fee += fee
        track.sell_tax += tax
        track.exit_reason = intent.reason or "closed"
        lot.apply_fill(Side.SELL, qty, price, fee, tax)
        if lot.state is PositionState.CLOSED:
            trades.append(self._make_trade(lot, track, bar))
        return cash

    @staticmethod
    def _make_trade(lot: PositionLot, track: _LotTrack, bar: Bar) -> BTTrade:
        entry_price = track.buy_notional / track.buy_qty if track.buy_qty > 0 else Decimal(0)
        exit_price = track.sell_notional / track.sell_qty if track.sell_qty > 0 else Decimal(0)
        net = (
            track.sell_notional - track.sell_fee - track.sell_tax - track.buy_notional - track.buy_fee
        )
        r: float | None = None
        if track.entry_stop is not None and entry_price > track.entry_stop:
            r = float((exit_price - entry_price) / (entry_price - track.entry_stop))
        return BTTrade(
            ticker=lot.ticker,
            market=lot.market.value,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=track.buy_qty,
            gross_pnl=track.sell_notional - track.buy_notional,
            fees=track.buy_fee + track.sell_fee,
            tax=track.sell_tax,
            net_pnl=net,
            r_multiple=r,
            opened_at=track.entry_ts or bar.ts,
            closed_at=bar.ts,
            exit_reason=track.exit_reason,
        )
