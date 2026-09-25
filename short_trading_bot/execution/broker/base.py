"""BrokerAdapter interface — one contract for domestic + overseas, paper + live.

Fills flow BACK to the engine via :pyattr:`fill_handler` (set by the OrderManager),
mirroring the plan's "broker order/fill events flow back onto the event bus".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from ..types import AccountBalance, Execution, FillHandler, OrderAck, OrderRecord, OrderRequest


class BrokerAdapter(ABC):
    #: Set by the OrderManager so the adapter can push fills back asynchronously.
    fill_handler: FillHandler | None = None
    #: False when the last ``get_executions`` returned without every broker leg answering
    #: (e.g. a routing adapter degraded a failed overseas leg to an empty list). Callers that
    #: need a *confirmed* refresh (post-cancel barriers) must treat that poll as failed.
    executions_complete: bool = True

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def submit_order(self, req: OrderRequest) -> OrderAck:
        """Submit an order. Implementations must be safe to call once per client_order_id."""

    @abstractmethod
    async def cancel_order(self, req: OrderRequest, broker_order_no: str | None) -> OrderAck: ...

    @abstractmethod
    async def get_balance(self) -> AccountBalance:
        """Authoritative account snapshot (cash + positions) used by the Reconciler."""

    @abstractmethod
    async def get_open_orders(self) -> list[OrderAck]:
        """Currently working (unfilled/partially-filled) orders at the broker."""

    async def get_executions(self) -> list[Execution]:
        """Authoritative executed-trade records (체결내역). Default empty; live adapters
        override. The FillPoller polls this and dedups by exec_id — ground-truth fills."""
        return []

    async def on_market_price(
        self,
        ticker: str,
        price: Decimal,
        *,
        low: Decimal | None = None,
        high: Decimal | None = None,
    ) -> None:
        """Latest market price (and the bar's low/high when known) for ``ticker``. Default
        no-op; the paper broker uses it to match resting limit orders
        (``PaperConfig.resting_limits``)."""
        return None

    async def get_daily_orders(self) -> list[OrderRecord]:
        """Today's broker-side orders, filled or not (주문내역). Default empty; live
        adapters override. The UnknownOrderResolver matches these against UNKNOWN local
        orders to recover a lost broker_order_no after a submit timeout."""
        return []
