from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from short_trading_bot.domain.enums import Market, OrderState, Side
from short_trading_bot.execution.broker.paper import PaperBrokerAdapter, PaperConfig
from short_trading_bot.execution.order_manager import OrderManager
from short_trading_bot.execution.types import OrderRequest
from short_trading_bot.persistence.db import session_scope
from short_trading_bot.persistence.models import Fill as FillRow
from short_trading_bot.persistence.models import Order, Position


async def _seed_position(
    sf: async_sessionmaker[AsyncSession], lot_id: str = "lot1", ticker: str = "005930"
) -> None:
    async with session_scope(sf) as s:
        s.add(
            Position(
                lot_id=lot_id,
                ticker=ticker,
                state="HOLDING",
                strategy_id="trend_long_v1",
                qty_target=Decimal("10"),
            )
        )


def _req(cid: str, side: Side, qty: str) -> OrderRequest:
    return OrderRequest(
        client_order_id=cid,
        lot_id="lot1",
        ticker="005930",
        market=Market.KRX,
        side=side,
        qty=Decimal(qty),
        ord_dvsn="01",
    )


async def _count(sf: async_sessionmaker[AsyncSession], model: type) -> int:
    async with session_scope(sf) as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def _position(sf: async_sessionmaker[AsyncSession], lot_id: str = "lot1") -> Position:
    async with session_scope(sf) as s:
        return (
            await s.execute(select(Position).where(Position.lot_id == lot_id))
        ).scalar_one()


async def test_buy_fill_updates_position_and_order_state(sf) -> None:
    await _seed_position(sf)
    broker = PaperBrokerAdapter(PaperConfig())
    broker.set_price("005930", "70000")
    om = OrderManager(broker, sf)

    ack = await om.submit(_req("c1", Side.BUY, "10"))
    assert ack.accepted

    pos = await _position(sf)
    assert pos.qty_filled == Decimal("10")
    assert pos.avg_entry_price == Decimal("70035")  # market buy w/ slippage

    async with session_scope(sf) as s:
        order = (await s.execute(select(Order).where(Order.client_order_id == "c1"))).scalar_one()
        assert order.state == OrderState.FILLED.value
        assert order.broker_order_no


async def test_submit_is_idempotent(sf) -> None:
    await _seed_position(sf)
    broker = PaperBrokerAdapter(PaperConfig())
    broker.set_price("005930", "70000")
    om = OrderManager(broker, sf)

    await om.submit(_req("dup", Side.BUY, "10"))
    await om.submit(_req("dup", Side.BUY, "10"))  # retry, same client_order_id

    assert await _count(sf, Order) == 1
    assert await _count(sf, FillRow) == 1  # broker not re-hit
    pos = await _position(sf)
    assert pos.qty_filled == Decimal("10")  # not doubled


async def test_sell_realizes_pnl(sf) -> None:
    await _seed_position(sf)
    broker = PaperBrokerAdapter(PaperConfig())
    broker.set_price("005930", "70000")
    om = OrderManager(broker, sf)
    await om.submit(_req("buy", Side.BUY, "10"))

    broker.set_price("005930", "80000")
    await om.submit(_req("sell", Side.SELL, "10"))

    pos = await _position(sf)
    assert pos.qty_filled == Decimal("0")
    assert pos.realized_pnl > 0  # sold higher than avg entry (net of fees/tax)


async def test_rejected_order_recorded(sf) -> None:
    await _seed_position(sf)
    broker = PaperBrokerAdapter(PaperConfig(initial_cash=Decimal("1000")))
    broker.set_price("005930", "70000")
    om = OrderManager(broker, sf)

    ack = await om.submit(_req("rej", Side.BUY, "10"))
    assert not ack.accepted

    async with session_scope(sf) as s:
        order = (
            await s.execute(select(Order).where(Order.client_order_id == "rej"))
        ).scalar_one()
        assert order.state == OrderState.REJECTED.value
    assert await _count(sf, FillRow) == 0
