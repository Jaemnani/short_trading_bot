import functools
from decimal import Decimal

from short_trading_bot.domain.enums import Currency, Market, Side
from short_trading_bot.execution.broker.paper import PaperBrokerAdapter, PaperConfig
from short_trading_bot.execution.types import Fill, OrderRequest


async def _collect(store: list[Fill], f: Fill) -> None:
    store.append(f)


def _broker(cfg: PaperConfig, store: list[Fill]) -> PaperBrokerAdapter:
    b = PaperBrokerAdapter(cfg)
    b.fill_handler = functools.partial(_collect, store)
    return b


def _req(side: Side, qty: str, price: str = "0", ord_dvsn: str = "01") -> OrderRequest:
    return OrderRequest(
        client_order_id=f"c-{side}-{qty}-{price}",
        lot_id="lot1",
        ticker="005930",
        market=Market.KRX,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        ord_dvsn=ord_dvsn,
    )


async def test_market_buy_slippage_and_cash() -> None:
    fills: list[Fill] = []
    b = _broker(PaperConfig(initial_cash=Decimal("1000000")), fills)
    b.set_price("005930", "70000")

    ack = await b.submit_order(_req(Side.BUY, "10"))

    assert ack.accepted and ack.broker_order_no
    assert len(fills) == 1
    assert fills[0].price == Decimal("70035")  # 70000 * (1 + 5bps)
    assert fills[0].qty == Decimal("10")
    assert fills[0].fee > 0 and fills[0].tax == 0  # buy: no securities tax
    # cash = 1,000,000 - notional(700350) - fee
    assert b.cash(Currency.KRW) < Decimal("300000")


async def test_limit_buy_fills_at_limit() -> None:
    fills: list[Fill] = []
    b = _broker(PaperConfig(), fills)
    ack = await b.submit_order(_req(Side.BUY, "5", price="68000", ord_dvsn="00"))
    assert ack.accepted
    assert fills[0].price == Decimal("68000")


async def test_sell_applies_krx_tax() -> None:
    fills: list[Fill] = []
    b = _broker(PaperConfig(), fills)
    b.set_price("005930", "70000")
    await b.submit_order(_req(Side.BUY, "10"))
    await b.submit_order(_req(Side.SELL, "10"))
    sell_fill = fills[-1]
    assert sell_fill.tax > 0  # KRX sell-side 증권거래세


async def test_partial_fills() -> None:
    fills: list[Fill] = []
    b = _broker(PaperConfig(max_fill_chunks=2), fills)
    b.set_price("005930", "70000")
    await b.submit_order(_req(Side.BUY, "10"))
    assert len(fills) == 2
    assert sum((f.qty for f in fills), Decimal(0)) == Decimal("10")


async def test_partial_fill_chunks_are_integral_for_integral_qty() -> None:
    """정수 수량 주문은 청크도 정수여야 한다 (41668/3 = 13889.33…주 금지)."""
    fills: list[Fill] = []
    b = _broker(PaperConfig(max_fill_chunks=3, enforce_funds=False), fills)
    b.set_price("005930", "70000")
    await b.submit_order(_req(Side.BUY, "41668"))
    assert [f.qty for f in fills] == [Decimal("13889"), Decimal("13889"), Decimal("13890")]

    # 청크 수보다 작은 주문은 0-수량 청크 없이 한 번에 체결
    fills.clear()
    await b.submit_order(_req(Side.SELL, "2"))
    assert [f.qty for f in fills] == [Decimal("2")]


async def test_insufficient_funds_rejected() -> None:
    fills: list[Fill] = []
    b = _broker(PaperConfig(initial_cash=Decimal("1000")), fills)
    b.set_price("005930", "70000")
    ack = await b.submit_order(_req(Side.BUY, "10"))
    assert not ack.accepted
    assert ack.reject_reason == "insufficient_funds"
    assert fills == []


async def test_market_order_without_price_rejected() -> None:
    fills: list[Fill] = []
    b = _broker(PaperConfig(), fills)
    ack = await b.submit_order(_req(Side.BUY, "10"))  # no set_price
    assert not ack.accepted
    assert ack.reject_reason == "no_market_price"


async def test_balance_reflects_holdings() -> None:
    fills: list[Fill] = []
    b = _broker(PaperConfig(), fills)
    b.set_price("005930", "70000")
    await b.submit_order(_req(Side.BUY, "10"))
    bal = await b.get_balance()
    assert len(bal.positions) == 1
    assert bal.positions[0].ticker == "005930"
    assert bal.positions[0].qty == Decimal("10")
