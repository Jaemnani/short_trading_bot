from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from short_trading_bot.domain.enums import Currency, Market
from short_trading_bot.execution.reconciler import Reconciler
from short_trading_bot.execution.types import (
    AccountBalance,
    BalancePosition,
    OrderAck,
    OrderRequest,
)
from short_trading_bot.persistence.db import session_scope
from short_trading_bot.persistence.models import Position


class StubBroker:
    """Minimal BrokerAdapter returning a crafted balance (broker = source of truth)."""

    fill_handler = None

    def __init__(self, positions: list[BalancePosition]) -> None:
        self._positions = positions

    @property
    def name(self) -> str:
        return "stub"

    async def submit_order(self, req: OrderRequest) -> OrderAck:  # pragma: no cover
        raise NotImplementedError

    async def cancel_order(self, req: OrderRequest, broker_order_no: str | None) -> OrderAck:
        raise NotImplementedError  # pragma: no cover

    async def get_balance(self) -> AccountBalance:
        return AccountBalance(cash={Currency.KRW: Decimal(0)}, positions=self._positions)

    async def get_open_orders(self) -> list[OrderAck]:
        return []


async def _seed(sf: async_sessionmaker[AsyncSession], ticker: str, qty: str) -> None:
    async with session_scope(sf) as s:
        s.add(
            Position(
                lot_id=f"lot-{ticker}",
                ticker=ticker,
                state="HOLDING",
                strategy_id="t",
                qty_filled=Decimal(qty),
            )
        )


def _bal(ticker: str, qty: str) -> BalancePosition:
    return BalancePosition(
        ticker=ticker, market=Market.KRX, qty=Decimal(qty), avg_price=Decimal("100"),
        currency=Currency.KRW,
    )


async def test_in_sync(sf) -> None:
    await _seed(sf, "005930", "10")
    rec = Reconciler(StubBroker([_bal("005930", "10")]), sf)
    report = await rec.reconcile()
    assert report.in_sync
    assert report.mismatches == []


async def test_detects_drift(sf) -> None:
    await _seed(sf, "005930", "10")
    rec = Reconciler(StubBroker([_bal("005930", "7")]), sf)  # broker has fewer
    report = await rec.reconcile()
    assert not report.in_sync
    (drift,) = report.mismatches
    assert drift.ticker == "005930"
    assert drift.delta == Decimal("-3")  # broker - local


async def test_position_missing_at_broker(sf) -> None:
    await _seed(sf, "000660", "5")
    rec = Reconciler(StubBroker([]), sf)  # broker reports nothing
    report = await rec.reconcile()
    assert not report.in_sync
    assert report.mismatches[0].delta == Decimal("-5")
