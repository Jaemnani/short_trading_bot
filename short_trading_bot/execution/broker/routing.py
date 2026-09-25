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

    async def _overseas_call(
        self, fetch: Callable[[], Awaitable[_T]], default: _T
    ) -> _T:
        """해외 레그 조회 격리 — 실패가 국내(핵심) 동작을 볼모로 잡지 않는다.

        모의 도메인의 해외 TR(inquire-ccnl·inquire-balance)은 간헐 500을 반환한다
        (2026-08-03 실측 — 잔고 500이 _equity→reconcile로 전파돼 엔진이 죽고 나흘간
        방치됐다). 실패 시 default로 강등하고, 연속 실패가 쌓이면 다음 해외 주문이
        나갈 때까지 해외 조회를 쉰다 (주기 호출의 에러 스팸·무의미 호출 방지).
        """
        if self._overseas_fail >= _OVERSEAS_FAIL_LIMIT:
            return default
        try:
            result = await fetch()
        except Exception:
            self._overseas_fail += 1
            self._log.warning(
                "overseas_call.failed", consecutive=self._overseas_fail,
                suspended=self._overseas_fail >= _OVERSEAS_FAIL_LIMIT,
            )
            return default
        self._overseas_fail = 0
        return result

    async def _overseas_read(self, fetch: Callable[[], Awaitable[list[_T]]]) -> list[_T]:
        empty: list[_T] = []
        return list(await self._overseas_call(fetch, empty))

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
            # 해외 잔고 실패 = 국내 잔고만으로 강등 (equity 과소 = 보수적 사이징이라 안전).
            other = await self._overseas_call(self._overseas.get_balance, None)
            if other is not None:
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
        self.executions_complete = True
        if self._overseas is not None:
            before = self._overseas_fail
            execs.extend(await self._overseas_read(self._overseas.get_executions))
            # 해외 레그 실패(또는 연속 실패로 조회 중단)는 빈 목록으로 강등된다 — 국내 폴링은
            # 계속 돌되, '체결 반영 확인'이 필요한 호출자에게는 미완료로 알린다.
            if self._overseas_fail > before or self._overseas_fail >= _OVERSEAS_FAIL_LIMIT:
                self.executions_complete = False
        return execs

    async def get_daily_orders(self) -> list[OrderRecord]:
        orders = list(await self._domestic.get_daily_orders())
        if self._overseas is not None:
            orders.extend(await self._overseas_read(self._overseas.get_daily_orders))
        return orders
