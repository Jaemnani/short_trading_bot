"""PositionLot — the dynamically-created, self-contained "주식객체".

Holds frozen params + a bound Strategy (composition) + its own state machine. Fills
drive state transitions; ``evaluate`` delegates to the strategy with a built context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..market.types import IndicatorSnapshot
from ..strategy.base import Strategy, StrategyContext
from .enums import Currency, Market, PositionState, Side
from .params import PositionParams
from .signal import Intent, IntentKind

_ALLOWED: dict[PositionState, frozenset[PositionState]] = {
    PositionState.WATCHING: frozenset(
        {PositionState.HOLDING, PositionState.CANCELLED, PositionState.ERROR}
    ),
    PositionState.HOLDING: frozenset(
        {PositionState.SCALING, PositionState.EXITING, PositionState.CLOSED, PositionState.ERROR}
    ),
    PositionState.SCALING: frozenset(
        {PositionState.HOLDING, PositionState.EXITING, PositionState.CLOSED, PositionState.ERROR}
    ),
    PositionState.EXITING: frozenset(
        {PositionState.CLOSED, PositionState.HOLDING, PositionState.ERROR}
    ),
    PositionState.ERROR: frozenset(
        {
            PositionState.HOLDING,
            PositionState.EXITING,
            PositionState.CLOSED,
            PositionState.CANCELLED,
        }
    ),
    PositionState.CLOSED: frozenset(),
    PositionState.CANCELLED: frozenset(),
}


class IllegalTransition(RuntimeError):
    pass


@dataclass
class PositionLot:
    lot_id: str
    ticker: str
    market: Market
    currency: Currency
    params: PositionParams
    strategy: Strategy
    state: PositionState = PositionState.WATCHING
    qty: Decimal = Decimal(0)
    avg_entry: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    peak_price: Decimal = Decimal(0)
    bars_held: int = 0
    initial_stop: Decimal | None = None
    original_qty: Decimal = Decimal(0)  # intended size at entry; basis for TP fractions
    tp_rungs_taken: int = 0
    _pending_stop: Decimal | None = field(default=None, repr=False)
    _pending_original_qty: Decimal | None = field(default=None, repr=False)

    # -- state machine ---------------------------------------------------

    def transition_to(self, new_state: PositionState) -> None:
        if new_state not in _ALLOWED[self.state]:
            raise IllegalTransition(f"{self.state} -> {new_state} not allowed")
        if new_state is PositionState.CLOSED and self.qty > 0:
            raise IllegalTransition(f"cannot CLOSE lot with qty={self.qty} (would orphan shares)")
        self.state = new_state

    def begin_exit(self) -> None:
        """Mark that a closing order is working (driven by the order layer)."""
        if self.state in (PositionState.HOLDING, PositionState.SCALING):
            self.transition_to(PositionState.EXITING)

    def mark_error(self) -> None:
        self.transition_to(PositionState.ERROR)

    def recover(self, to: PositionState = PositionState.HOLDING) -> None:
        """Leave ERROR after a reconcile/repair."""
        self.transition_to(to)

    @property
    def is_open(self) -> bool:
        return self.state in (PositionState.HOLDING, PositionState.SCALING, PositionState.EXITING)

    @property
    def is_terminal(self) -> bool:
        return self.state in (PositionState.CLOSED, PositionState.CANCELLED)

    # -- evaluation ------------------------------------------------------

    def evaluate(
        self,
        snapshot: IndicatorSnapshot,
        equity: Decimal,
        *,
        prev: IndicatorSnapshot | None = None,
        news_ewma: float | None = None,
        now: datetime | None = None,
    ) -> list[Intent]:
        if snapshot.resolution is not self.params.resolution:
            # Never trade on bars of a different timeframe than this lot was configured for.
            return [Intent(kind=IntentKind.HOLD, side=Side.BUY, reason="resolution_mismatch")]
        if snapshot.bar_count < self.strategy.warmup_bars:
            return [Intent(kind=IntentKind.HOLD, side=Side.BUY, reason="warming_up")]

        ctx = StrategyContext(
            snapshot=snapshot,
            state=self.state,
            qty=self.qty,
            avg_entry=self.avg_entry,
            peak_price=self.peak_price if self.peak_price > 0 else snapshot.close,
            bars_held=self.bars_held,
            params=self.params,
            equity=equity,
            initial_stop=self.initial_stop,
            original_qty=self.original_qty,
            tp_rungs_taken=self.tp_rungs_taken,
            prev=prev,
            news_ewma=news_ewma,
            now=now,
        )
        intents = self.strategy.evaluate(ctx)
        for intent in intents:
            if intent.kind is IntentKind.ENTER:
                if intent.stop_price is not None:
                    self._pending_stop = intent.stop_price  # applied on the entry fill
                if intent.qty is not None:
                    self._pending_original_qty = intent.qty
            elif intent.kind is IntentKind.TRIM and intent.reason.startswith("take_profit"):
                # Advance the ladder so the same rung cannot re-fire. (P5 will key this to
                # the TRIM fill; paper fills are immediate so emit-time advancement is exact.)
                self.record_tp_rung()
        return intents

    def on_bar(self, high: Decimal) -> None:
        """Advance time: track bars held and the peak HIGH (for the chandelier stop)."""
        if self.is_open:
            self.bars_held += 1
            if high > self.peak_price:
                self.peak_price = high

    def record_tp_rung(self) -> None:
        self.tp_rungs_taken += 1

    def rebase_entry_qty(self, qty: Decimal) -> None:
        """진입 수량이 리스크 캡으로 축소됐을 때 호출: TP 분할(fraction) 기준을
        전략이 의도한 수량이 아니라 실제 주문 수량으로 재설정한다. 이걸 빼먹으면
        원래 의도 수량 기준 fraction이 실제 보유량을 초과해 첫 TP에서 전량 매도된다."""
        self._pending_original_qty = qty

    # -- fills -----------------------------------------------------------

    def apply_fill(
        self,
        side: Side,
        qty: Decimal,
        price: Decimal,
        fee: Decimal = Decimal(0),
        tax: Decimal = Decimal(0),
        *,
        is_add: bool = False,
    ) -> None:
        if side is Side.BUY:
            new_qty = self.qty + qty
            if new_qty > 0:
                self.avg_entry = (self.avg_entry * self.qty + price * qty) / new_qty
            self.qty = new_qty
            if self.state is PositionState.WATCHING:
                self.transition_to(PositionState.HOLDING)
                if self.peak_price <= 0:
                    self.peak_price = price
                if self._pending_stop is not None:
                    self.initial_stop = self._pending_stop
                self.original_qty = (
                    self._pending_original_qty if self._pending_original_qty is not None else qty
                )
            elif is_add and self.state is PositionState.HOLDING:
                self.transition_to(PositionState.SCALING)  # 분할매수 / pyramid
        else:  # SELL closes part/all of the long -> realize P&L on the held portion only
            realized_qty = min(qty, self.qty) if self.qty > 0 else Decimal(0)
            self.realized_pnl += (price - self.avg_entry) * realized_qty - fee - tax
            self.qty = max(Decimal(0), self.qty - qty)
            if self.qty == 0:
                self.transition_to(PositionState.CLOSED)
