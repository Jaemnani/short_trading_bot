from decimal import Decimal
from typing import Any

import pytest

from short_trading_bot.domain.enums import Currency, Market, Mode, Side
from short_trading_bot.execution.broker.kis_overseas import KisOverseasAdapter
from short_trading_bot.execution.broker.market_router import MarketRouter
from short_trading_bot.execution.fx import FxManager, FxRates
from short_trading_bot.execution.types import OrderRequest
from short_trading_bot.infra.config import KisEnvCreds
from short_trading_bot.infra.kis_auth import KisAuth

# --- MarketRouter ---

ROUTER = MarketRouter()


def test_domestic_order_tr_ids() -> None:
    assert ROUTER.order_tr_id(Market.KRX, Side.BUY, Mode.LIVE) == "TTTC0802U"
    assert ROUTER.order_tr_id(Market.KRX, Side.SELL, Mode.LIVE) == "TTTC0801U"
    assert ROUTER.order_tr_id(Market.KRX, Side.BUY, Mode.PAPER) == "VTTC0802U"


def test_overseas_order_tr_ids() -> None:
    assert ROUTER.order_tr_id(Market.NASD, Side.BUY, Mode.LIVE) == "TTTT1002U"
    assert ROUTER.order_tr_id(Market.NASD, Side.SELL, Mode.LIVE) == "TTTT1006U"
    assert ROUTER.order_tr_id(Market.NASD, Side.BUY, Mode.PAPER) == "VTTT1002U"
    assert ROUTER.order_tr_id(Market.SEHK, Side.BUY, Mode.LIVE) == "TTTS1002U"
    assert ROUTER.order_tr_id(Market.TKSE, Side.SELL, Mode.PAPER) == "VTTS0307U"


def test_exchange_codes_distinct() -> None:
    assert ROUTER.trading_exchange_code(Market.NASD) == "NASD"  # OVRS_EXCG_CD
    assert ROUTER.price_exchange_code(Market.NASD) == "NAS"  # EXCD
    assert ROUTER.price_exchange_code(Market.KRX) is None


# --- FxManager ---

def test_fx_krw_conversion_and_buying_power() -> None:
    rates = FxRates(to_krw={Currency.USD: Decimal("1350")})
    fx = FxManager(integrated_margin=True)
    balances = {Currency.KRW: Decimal("1000000"), Currency.USD: Decimal("100")}

    assert fx.to_krw(Decimal("100"), Currency.USD, rates) == Decimal("135000")
    assert fx.total_krw(balances, rates) == Decimal("1135000")
    # integrated margin: full KRW-equivalent funds an overseas buy
    assert fx.available_for_overseas(balances, rates, Market.NASD) == Decimal("1135000")


def test_fx_without_integrated_margin_uses_only_market_currency() -> None:
    rates = FxRates(to_krw={Currency.USD: Decimal("1350")})
    fx = FxManager(integrated_margin=False)
    balances = {Currency.KRW: Decimal("1000000"), Currency.USD: Decimal("100")}
    assert fx.available_for_overseas(balances, rates, Market.NASD) == Decimal("135000")  # USD only


def test_on_demand_conversion_unavailable() -> None:
    result = FxManager().request_conversion(Decimal("100"), Currency.KRW, Currency.USD)
    assert result.executed is False
    assert "환전" in result.reason or "FX" in result.reason


# --- KisOverseasAdapter (injected transport) ---

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


def _adapter(transport: FakeTransport) -> KisOverseasAdapter:
    return KisOverseasAdapter(
        _auth(),
        _creds(),
        "https://openapivts.koreainvestment.com:29443",
        Mode.PAPER,
        transport=transport,
    )


def _buy_req() -> OrderRequest:
    return OrderRequest(
        client_order_id="c1", lot_id="l1", ticker="AAPL", market=Market.NASD,
        side=Side.BUY, qty=Decimal("10"), price=Decimal("150"), ord_dvsn="00",
    )


async def test_submit_order_builds_request_and_parses_ack() -> None:
    transport = FakeTransport({"rt_cd": "0", "msg1": "ok", "output": {"ODNO": "0000123"}})
    ack = await _adapter(transport).submit_order(_buy_req())

    assert ack.accepted and ack.broker_order_no == "0000123"
    assert ack.tr_id == "VTTT1002U"  # paper US buy

    method, url, headers, body = transport.calls[0]
    assert method == "POST" and url.endswith("/uapi/overseas-stock/v1/trading/order")
    assert headers["tr_id"] == "VTTT1002U"
    assert headers["authorization"] == "Bearer tok123"
    assert headers["appkey"] == "ak"
    assert body["OVRS_EXCG_CD"] == "NASD"
    assert body["PDNO"] == "AAPL"
    assert body["ORD_QTY"] == "10"
    assert body["OVRS_ORD_UNPR"] == "150"
    assert body["CANO"] == "12345678"


async def test_submit_order_rejection() -> None:
    transport = FakeTransport({"rt_cd": "1", "msg1": "주문가능금액부족"})
    ack = await _adapter(transport).submit_order(_buy_req())
    assert not ack.accepted
    assert ack.reject_reason == "주문가능금액부족"


async def test_get_balance_parses_holdings_and_cash() -> None:
    transport = FakeTransport(
        {
            "output1": [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_cblc_qty": "5",
                    "pchs_avg_pric": "150.5",
                    "tr_crcy_cd": "USD",
                    "ovrs_excg_cd": "NASD",
                }
            ],
            "output2": {"frcr_dncl_amt1": "1000.50"},
        }
    )
    bal = await _adapter(transport).get_balance()
    assert len(bal.positions) == 1
    pos = bal.positions[0]
    assert pos.ticker == "AAPL" and pos.qty == Decimal("5")
    assert pos.currency is Currency.USD
    assert bal.cash[Currency.USD] == Decimal("1000.50")


async def test_cancel_order_unimplemented() -> None:
    with pytest.raises(NotImplementedError):
        await _adapter(FakeTransport({})).cancel_order(_buy_req(), "0000123")
