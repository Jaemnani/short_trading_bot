"""RoutingBrokerAdapter — one BrokerAdapter that dispatches by market.

Domestic (KRX) orders go to the KIS domestic adapter; overseas orders to the KIS overseas
adapter. Balances are merged. The fill handler is propagated to both children so all fills
reach the OrderManager. This lets the TradingService trade 국내 + 해외 through one interface.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import TypeVar

from ...domain.enums import Market
from ...infra.logging import get_logger
from ..types import AccountBalance, Execution, FillHandler, OrderAck, OrderRecord, OrderRequest
from .base import BrokerAdapter

_T = TypeVar("_T")

# 해외 read 폴링 연속 실패 상한 — 도달하면 다음 해외 주문까지 해외 폴링을 쉰다.
_OVERSEAS_FAIL_LIMIT = 3


class RoutingBrokerAdapter(BrokerAdapter):
    def __init__(self, domestic: BrokerAdapter, overseas: BrokerAdapter | None = None) -> None:
        self._domestic = domestic
        self._overseas = overseas
        self._handler: FillHandler | None = None
        self._overseas_fail = 0
        self._log = get_logger("routing_broker")

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

    async def _overseas_read(self, fetch: Callable[[], Awaitable[list[_T]]]) -> list[_T]:
        """해외 레그 read 집계 — 실패를 격리해 국내(핵심) 체결 배달을 볼모로 잡지 않는다.

        모의 도메인은 해외 체결내역 TR(inquire-ccnl)에 500을 반환한다 (2026-08-03 실측) —
        예외가 전파되면 국내 체결 폴링까지 통째로 죽는다. 연속 실패가 쌓이면 다음 해외
        주문이 나갈 때까지 해외 폴링을 쉰다 (2초 주기 에러 스팸·무의미 호출 방지).
        """
        if self._overseas_fail >= _OVERSEAS_FAIL_LIMIT:
            return []
        try:
            result = await fetch()
        except Exception:
            self._overseas_fail += 1
            self._log.warning(
                "overseas_read.failed", consecutive=self._overseas_fail,
                suspended=self._overseas_fail >= _OVERSEAS_FAIL_LIMIT,
            )
            return []
        self._overseas_fail = 0
        return list(result)

    async def submit_order(self, req: OrderRequest) -> OrderAck:
        if req.market.is_overseas:
            self._overseas_fail = 0  # 해외 주문이 나가면 해외 폴링을 다시 살린다
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
            orders.extend(await self._overseas_read(self._overseas.get_open_orders))
        return orders

    async def get_executions(self) -> list[Execution]:
        execs = list(await self._domestic.get_executions())
        if self._overseas is not None:
            execs.extend(await self._overseas_read(self._overseas.get_executions))
        return execs

    async def get_daily_orders(self) -> list[OrderRecord]:
        orders = list(await self._domestic.get_daily_orders())
        if self._overseas is not None:
            orders.extend(await self._overseas_read(self._overseas.get_daily_orders))
        return orders
