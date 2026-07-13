"""Broker-facing value objects (not persisted; the DB models mirror these).

Monetary/quantity fields use ``Decimal`` for exact accounting.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..domain.enums import Currency, Market, Side


@dataclass(slots=True)
class OrderRequest:
    """An intent to place an order, already carrying its idempotency key."""

    client_order_id: str
    lot_id: str
    ticker: str
    market: Market
    side: Side
    qty: Decimal
    price: Decimal = Decimal(0)  # 0 => market order
    ord_dvsn: str = "00"  # KIS domestic: 00 limit, 01 market
    tif: str = "DAY"

    @property
    def is_market(self) -> bool:
        return self.ord_dvsn == "01" or self.price <= 0

    @property
    def notional(self) -> Decimal:
        return self.price * self.qty


@dataclass(slots=True)
class OrderAck:
    """Broker response to a submit/cancel."""

    client_order_id: str
    accepted: bool
    broker_order_no: str | None = None
    tr_id: str | None = None
    reject_reason: str | None = None


@dataclass(slots=True)
class Fill:
    """A (partial) execution. ``fee``/``tax`` are in the position currency."""

    client_order_id: str
    qty: Decimal
    price: Decimal
    fee: Decimal = Decimal(0)
    tax: Decimal = Decimal(0)
    currency: Currency = Currency.KRW
    ts: datetime | None = None
    source: str = "ws"  # ws | reconcile | paper


@dataclass(slots=True)
class Execution:
    """An authoritative executed-trade record from the broker (체결내역) — ground truth
    for fills, including exact fee/tax. ``qty``, ``fee`` and ``tax`` are CUMULATIVE per
    order and ``price`` is the cumulative average (KIS 체결내역 semantics); the FillPoller
    derives per-fill deltas against the DB. NOTE: the KIS adapters currently leave
    fee/tax at 0 (the API rows don't carry them) — only the paper broker populates them."""

    exec_id: str
    broker_order_no: str
    ticker: str
    side: Side
    qty: Decimal
    price: Decimal
    fee: Decimal = Decimal(0)
    tax: Decimal = Decimal(0)
    currency: Currency = Currency.KRW
    ts: datetime | None = None


@dataclass(slots=True)
class BalancePosition:
    ticker: str
    market: Market
    qty: Decimal
    avg_price: Decimal
    currency: Currency


@dataclass(slots=True)
class AccountBalance:
    """Broker-reported balance — the source of truth for reconciliation."""

    cash: dict[Currency, Decimal] = field(default_factory=dict)
    positions: list[BalancePosition] = field(default_factory=list)


# A coroutine that consumes a fill pushed back from the broker.
FillHandler = Callable[[Fill], Awaitable[None]]
