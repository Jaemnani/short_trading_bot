"""RoutingBrokerAdapter — one BrokerAdapter that dispatches by market.

Domestic (KRX) orders go to the KIS domestic adapter; overseas orders to the KIS overseas
adapter. Balances are merged. The fill handler is propagated to both children so all fills
reach the OrderManager. This lets the TradingService trade 국내 + 해외 through one interface.
"""

from __future__ import annotations

from decimal import Decimal

from ...domain.enums import Market
from ..types import AccountBalance, Execution, FillHandler, OrderAck, OrderRecord, OrderRequest
from .base import BrokerAdapter


class RoutingBrokerAdapter(BrokerAdapter):
    def __init__(self, domestic: BrokerAdapter, overseas: BrokerAdapter | None = None) -> None:
        self._domestic = domestic
        self._overseas = overseas
        self._handler: FillHandler | None = None

    @property
    def name(self) -> str:
        return "routing"

    @property
    def fill_handler(self) -> FillHandler | None:
        return self._handler

    @fill_handler.setter
    def fill_handler(self, handler: FillHandler | None) -> None:
        self._handler = handler
        self._domestic.fill_handler = handler
        if self._overseas is not None:
            self._overseas.fill_handler = handler

    def _for(self, market: Market) -> BrokerAdapter:
        if not market.is_overseas:
            return self._domestic
        if self._overseas is None:
            raise ValueError("overseas broker not configured")
        return self._overseas

    async def submit_order(self, req: OrderRequest) -> OrderAck:
        return await self._for(req.market).submit_order(req)

    async def cancel_order(self, req: OrderRequest, broker_order_no: str | None) -> OrderAck:
        return await self._for(req.market).cancel_order(req, broker_order_no)

    async def get_balance(self) -> AccountBalance:
        balance = await self._domestic.get_balance()
        cash = dict(balance.cash)
        positions = list(balance.positions)
        if self._overseas is not None:
            other = await self._overseas.get_balance()
            for currency, amount in other.cash.items():
                cash[currency] = cash.get(currency, Decimal(0)) + amount
            positions.extend(other.positions)
        return AccountBalance(cash=cash, positions=positions)

    async def get_open_orders(self) -> list[OrderAck]:
        orders = list(await self._domestic.get_open_orders())
        if self._overseas is not None:
            orders.extend(await self._overseas.get_open_orders())
        return orders

    async def get_executions(self) -> list[Execution]:
        execs = list(await self._domestic.get_executions())
        if self._overseas is not None:
            execs.extend(await self._overseas.get_executions())
        return execs

    async def get_daily_orders(self) -> list[OrderRecord]:
        orders = list(await self._domestic.get_daily_orders())
        if self._overseas is not None:
            orders.extend(await self._overseas.get_daily_orders())
        return orders
