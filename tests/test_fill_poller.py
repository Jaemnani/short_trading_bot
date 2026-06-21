from decimal import Decimal

from short_trading_bot.domain.enums import Market, Side
from short_trading_bot.execution.broker.paper import PaperBrokerAdapter, PaperConfig
from short_trading_bot.execution.fill_poller import FillPoller
from short_trading_bot.execution.types import Fill, OrderRequest
from short_trading_bot.persistence.db import session_scope
from short_trading_bot.persistence.models import Order


def _buy(cid: str = "c1") -> OrderRequest:
    return OrderRequest(
        client_order_id=cid, lot_id="lot1", ticker="005930", market=Market.KRX,
        side=Side.BUY, qty=Decimal("10"), price=Decimal("70000"), ord_dvsn="00",
    )


async def _seed_order(sf, broker_order_no: str, cid: str = "c1") -> None:
    async with session_scope(sf) as s:
        s.add(
            Order(
                order_id=f"o-{cid}", lot_id="lot1", client_order_id=cid,
                broker_order_no=broker_order_no, side="BUY", qty=Decimal("10"),
                price=Decimal("70000"), state="NEW",
            )
        )


async def test_paper_records_executions() -> None:
    broker = PaperBrokerAdapter(PaperConfig())  # no fill_handler -> just records executions
    await broker.submit_order(_buy())
    execs = await broker.get_executions()
    assert len(execs) == 1
    assert execs[0].broker_order_no == "PAPER-00000001"
    assert execs[0].ticker == "005930" and execs[0].qty == Decimal("10")


async def test_fill_poller_resolves_and_dedups(sf) -> None:
    broker = PaperBrokerAdapter(PaperConfig())
    await broker.submit_order(_buy())  # records execution under broker_order_no PAPER-00000001
    await _seed_order(sf, "PAPER-00000001")

    collected: list[Fill] = []

    async def handler(f: Fill) -> None:
        collected.append(f)

    poller = FillPoller(broker, sf, handler)
    assert await poller.poll_once() == 1
    assert collected[0].client_order_id == "c1"
    assert collected[0].qty == Decimal("10") and collected[0].source == "poll"
    assert await poller.poll_once() == 0  # deduped by exec_id


async def test_fill_poller_skips_unresolved(sf) -> None:
    broker = PaperBrokerAdapter(PaperConfig())
    await broker.submit_order(_buy())  # execution exists but no matching Order row seeded

    collected: list[Fill] = []

    async def handler(f: Fill) -> None:
        collected.append(f)

    poller = FillPoller(broker, sf, handler)
    assert await poller.poll_once() == 0  # broker_order_no not resolvable -> skipped
    assert collected == []
