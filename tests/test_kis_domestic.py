from decimal import Decimal
from typing import Any

from short_trading_bot.domain.enums import Currency, Market, Mode, Side
from short_trading_bot.execution.broker.kis import KisBrokerAdapter
from short_trading_bot.execution.types import OrderRequest
from short_trading_bot.infra.config import KisEnvCreds
from short_trading_bot.infra.kis_auth import KisAuth


class FakeTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, str], dict[str, Any]]] = []

    async def __call__(
        self, method: str, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((method, url, headers, payload))
        return self.response


def _creds() -> KisEnvCreds:
    return KisEnvCreds(app_key="ak", app_secret="as", account_no="12345678-01")


def _auth() -> KisAuth:
    async def fetch() -> tuple[str, int]:
        return "tok123", 86400

    return KisAuth(_creds(), "https://openapivts.koreainvestment.com:29443", token_fetcher=fetch)


def _adapter(transport: FakeTransport) -> KisBrokerAdapter:
    return KisBrokerAdapter(
        _auth(), _creds(), "https://openapivts.koreainvestment.com:29443", Mode.PAPER, transport=transport
    )


def _buy() -> OrderRequest:
    return OrderRequest(
        client_order_id="c1", lot_id="l1", ticker="005930", market=Market.KRX,
        side=Side.BUY, qty=Decimal("10"), price=Decimal("70000"), ord_dvsn="00",
    )


async def test_domestic_submit_builds_request() -> None:
    transport = FakeTransport({"rt_cd": "0", "msg1": "ok", "output": {"ODNO": "0000999"}})
    ack = await _adapter(transport).submit_order(_buy())
    assert ack.accepted and ack.broker_order_no == "0000999"
    assert ack.tr_id == "VTTC0802U"  # paper domestic buy

    method, url, headers, body = transport.calls[0]
    assert method == "POST" and url.endswith("/uapi/domestic-stock/v1/trading/order-cash")
    assert headers["tr_id"] == "VTTC0802U"
    assert body["PDNO"] == "005930"
    assert body["ORD_QTY"] == "10"
    assert body["ORD_UNPR"] == "70000"
    assert body["CANO"] == "12345678"


async def test_domestic_rejection() -> None:
    transport = FakeTransport({"rt_cd": "1", "msg1": "주문수량초과"})
    ack = await _adapter(transport).submit_order(_buy())
    assert not ack.accepted and ack.reject_reason == "주문수량초과"


async def test_domestic_balance_parse() -> None:
    transport = FakeTransport(
        {
            "output1": [{"pdno": "005930", "hldg_qty": "10", "pchs_avg_pric": "70000"}],
            "output2": [{"dnca_tot_amt": "5000000"}],
        }
    )
    bal = await _adapter(transport).get_balance()
    assert bal.positions[0].ticker == "005930" and bal.positions[0].qty == Decimal("10")
    assert bal.cash[Currency.KRW] == Decimal("5000000")


async def test_domestic_cancel_builds_rvsecncl() -> None:
    transport = FakeTransport({"rt_cd": "0", "msg1": "ok"})
    ack = await _adapter(transport).cancel_order(_buy(), "0000999")
    assert ack.accepted
    _method, url, _headers, body = transport.calls[0]
    assert url.endswith("/uapi/domestic-stock/v1/trading/order-rvsecncl")
    assert body["RVSE_CNCL_DVSN_CD"] == "02"
    assert body["ORGN_ODNO"] == "0000999"
