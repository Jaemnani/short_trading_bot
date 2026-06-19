"""Simulated broker for backtest/paper. Same interface and code paths as live.

Models slippage, optional partial fills, fees, and a sell-side tax (KRX). Fills are
emitted via ``fill_handler`` immediately after acceptance (deterministic for tests).
Set reference prices with :meth:`set_price` before submitting market orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ...domain.enums import Currency, Market, Side
from ...infra.logging import get_logger
from ..types import AccountBalance, BalancePosition, Fill, OrderAck, OrderRequest
from .base import BrokerAdapter

_BPS = Decimal(10000)


@dataclass(slots=True)
class PaperConfig:
    initial_cash: Decimal = Decimal("100000000")  # 1억 KRW
    slippage_bps: Decimal = Decimal("5")  # market-order slippage
    fee_bps: Decimal = Decimal("1.5")  # ~0.015% brokerage
    sell_tax_bps: Decimal = Decimal("18")  # ~0.18% KRX 증권거래세 (sell only)
    max_fill_chunks: int = 1  # >1 => simulate partial fills
    enforce_funds: bool = True


@dataclass(slots=True)
class _Holding:
    qty: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)


class PaperBrokerAdapter(BrokerAdapter):
    def __init__(self, config: PaperConfig | None = None, logger: Any = None) -> None:
        self.config = config or PaperConfig()
        self._cash: dict[Currency, Decimal] = {Currency.KRW: self.config.initial_cash}
        self._holdings: dict[tuple[str, Market], _Holding] = {}
        self._prices: dict[str, Decimal] = {}
        self._seq = 0
        self._log = logger or get_logger("paper_broker")

    @property
    def name(self) -> str:
        return "paper"

    def set_price(self, ticker: str, price: Decimal | str | int | float) -> None:
        self._prices[ticker] = Decimal(str(price))

    def cash(self, currency: Currency = Currency.KRW) -> Decimal:
        return self._cash.get(currency, Decimal(0))

    async def submit_order(self, req: OrderRequest) -> OrderAck:
        ref = self._prices.get(req.ticker)
        if req.is_market and ref is None:
            return OrderAck(req.client_order_id, accepted=False, reject_reason="no_market_price")

        fill_price = self._fill_price(req, ref)
        ccy = req.market.currency

        if (
            req.side == Side.BUY
            and self.config.enforce_funds
            and self._estimated_cost(fill_price, req.qty) > self._cash.get(ccy, Decimal(0))
        ):
            return OrderAck(req.client_order_id, accepted=False, reject_reason="insufficient_funds")

        self._seq += 1
        broker_no = f"PAPER-{self._seq:08d}"

        for qty in self._chunk(req.qty):
            await self._apply_and_emit(req, fill_price, qty, ccy)

        return OrderAck(req.client_order_id, accepted=True, broker_order_no=broker_no, tr_id="PAPER")

    async def cancel_order(self, req: OrderRequest, broker_order_no: str | None) -> OrderAck:
        # Paper fills immediately, so there is nothing working to cancel.
        return OrderAck(req.client_order_id, accepted=True, broker_order_no=broker_order_no)

    async def get_balance(self) -> AccountBalance:
        positions = [
            BalancePosition(
                ticker=ticker,
                market=market,
                qty=h.qty,
                avg_price=h.avg_price,
                currency=market.currency,
            )
            for (ticker, market), h in self._holdings.items()
            if h.qty != 0
        ]
        return AccountBalance(cash=dict(self._cash), positions=positions)

    async def get_open_orders(self) -> list[OrderAck]:
        return []

    # -- internals -------------------------------------------------------

    def _fill_price(self, req: OrderRequest, ref: Decimal | None) -> Decimal:
        if not req.is_market:
            return req.price
        assert ref is not None
        slip = ref * self.config.slippage_bps / _BPS
        return ref + slip if req.side == Side.BUY else ref - slip

    def _estimated_cost(self, price: Decimal, qty: Decimal) -> Decimal:
        notional = price * qty
        return notional + notional * self.config.fee_bps / _BPS

    def _chunk(self, qty: Decimal) -> list[Decimal]:
        chunks = max(1, self.config.max_fill_chunks)
        if chunks == 1:
            return [qty]
        out: list[Decimal] = []
        remaining = qty
        for i in range(chunks):
            q = remaining if i == chunks - 1 else qty / Decimal(chunks)
            out.append(q)
            remaining -= q
        return out

    async def _apply_and_emit(
        self, req: OrderRequest, price: Decimal, qty: Decimal, ccy: Currency
    ) -> None:
        notional = price * qty
        fee = notional * self.config.fee_bps / _BPS
        tax = (
            notional * self.config.sell_tax_bps / _BPS
            if req.side == Side.SELL and req.market == Market.KRX
            else Decimal(0)
        )

        key = (req.ticker, req.market)
        holding = self._holdings.setdefault(key, _Holding())
        if req.side == Side.BUY:
            new_qty = holding.qty + qty
            if new_qty > 0:
                holding.avg_price = (holding.avg_price * holding.qty + price * qty) / new_qty
            holding.qty = new_qty
            self._cash[ccy] = self._cash.get(ccy, Decimal(0)) - notional - fee
        else:
            holding.qty -= qty
            self._cash[ccy] = self._cash.get(ccy, Decimal(0)) + notional - fee - tax

        fill = Fill(
            client_order_id=req.client_order_id,
            qty=qty,
            price=price,
            fee=fee,
            tax=tax,
            currency=ccy,
            ts=datetime.now(UTC),
            source="paper",
        )
        if self.fill_handler is not None:
            await self.fill_handler(fill)
        else:
            self._log.debug("paper.fill.no_handler", client_order_id=req.client_order_id)
