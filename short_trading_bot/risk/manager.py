"""RiskManager — the mandatory pre-trade gate every Intent passes before becoming an order.

Risk-reducing intents (EXIT/TRIM) always pass. New entries (ENTER/ADD) are blocked when
paused/stopped or when a limit would be breached. Forced liquidation (kill switch) bypasses
this gate entirely.
"""

from __future__ import annotations

from decimal import Decimal

from ..domain.signal import IntentKind
from .control import ControlSwitch
from .limits import RiskDecision, RiskLimits, RiskSnapshot

_ENTRY_KINDS = (IntentKind.ENTER, IntentKind.ADD)


class RiskManager:
    def __init__(self, limits: RiskLimits, control: ControlSwitch | None = None) -> None:
        self._limits = limits
        self._control = control or ControlSwitch()

    @property
    def control(self) -> ControlSwitch:
        return self._control

    def check(
        self,
        *,
        intent_kind: IntentKind,
        ticker: str,
        order_notional: Decimal,
        snapshot: RiskSnapshot,
    ) -> RiskDecision:
        # Exits/trims reduce risk -> always allowed (even when paused/stopped).
        if intent_kind not in _ENTRY_KINDS:
            return RiskDecision.allow()

        if self._control.is_stopped:
            return RiskDecision.block("stopped")
        if self._control.is_paused:
            return RiskDecision.block("paused")

        limits = self._limits
        if limits.daily_loss_limit is not None and snapshot.daily_pnl <= -limits.daily_loss_limit:
            return RiskDecision.block("daily_loss_limit")
        if (
            limits.max_open_positions is not None
            and snapshot.open_positions >= limits.max_open_positions
        ):
            return RiskDecision.block("max_open_positions")
        if limits.max_order_notional is not None and order_notional > limits.max_order_notional:
            return RiskDecision.block("order_notional")
        if limits.max_ticker_exposure is not None:
            projected = snapshot.ticker_exposure.get(ticker, Decimal(0)) + order_notional
            if projected > limits.max_ticker_exposure:
                return RiskDecision.block("ticker_exposure")

        return RiskDecision.allow()
