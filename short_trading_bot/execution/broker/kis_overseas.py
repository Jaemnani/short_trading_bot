"""KIS overseas-stock broker adapter (REST).

Implements :class:`BrokerAdapter` for 미국/홍콩/일본/중국/베트남. The HTTP transport is
injectable so request-building + response-parsing are unit-testable without network.
Fills are not pushed by KIS REST; the runtime delivers them via the overseas WebSocket
(HDFSCNT0 / H0GSCNI0) or by polling. ``cancel_order`` is implemented for US (TTTT1004U,
verified); non-US markets raise via the router until their cancel TR_IDs are confirmed.

⚠️ Response field names (ovrs_pdno, ovrs_cblc_qty, ...) follow common KIS shapes but must
be verified against the official sample repo / portal before live use.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx

from ...domain.enums import Currency, Market, Mode
from ...infra.config import KisEnvCreds
from ...infra.kis_auth import KisAuth
from ..types import AccountBalance, BalancePosition, OrderAck, OrderRequest
from .base import BrokerAdapter
from .market_router import MarketRouter

Transport = Callable[[str, str, dict[str, str], dict[str, Any]], Awaitable[dict[str, Any]]]

_ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
_CANCEL_PATH = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
_BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-balance"
_NCCS_PATH = "/uapi/overseas-stock/v1/trading/inquire-nccs"


class KisOverseasAdapter(BrokerAdapter):
    def __init__(
        self,
        auth: KisAuth,
        creds: KisEnvCreds,
        base_url: str,
        mode: Mode,
        router: MarketRouter | None = None,
        *,
        default_market: Market = Market.NASD,
        transport: Transport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._auth = auth
        self._creds = creds
        self._base = base_url.rstrip("/")
        self._mode = mode
        self._router = router or MarketRouter()
        self._default_market = default_market
        self._transport = transport or self._default_transport
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "kis_overseas"

    async def submit_order(self, req: OrderRequest) -> OrderAck:
        tr_id = self._router.order_tr_id(req.market, req.side, self._mode)
        headers = await self._headers(tr_id)
        body = self.build_order_body(req)
        resp = await self._transport("POST", f"{self._base}{_ORDER_PATH}", headers, body)
        if str(resp.get("rt_cd")) == "0":
            output = resp.get("output") or {}
            return OrderAck(
                client_order_id=req.client_order_id,
                accepted=True,
                broker_order_no=str(output.get("ODNO", "")) or None,
                tr_id=tr_id,
            )
        return OrderAck(
            client_order_id=req.client_order_id,
            accepted=False,
            tr_id=tr_id,
            reject_reason=str(resp.get("msg1", "rejected")),
        )

    async def cancel_order(self, req: OrderRequest, broker_order_no: str | None) -> OrderAck:
        # US verified (TTTT1004U); non-US markets raise (cancel TR unverified) via the router.
        tr_id = self._router.cancel_tr_id(req.market, self._mode)
        headers = await self._headers(tr_id)
        body = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._creds.account_product_code,
            "OVRS_EXCG_CD": self._router.trading_exchange_code(req.market),
            "PDNO": req.ticker,
            "ORGN_ODNO": broker_order_no or "",
            "RVSE_CNCL_DVSN_CD": "02",  # 02 = cancel
            "ORD_QTY": str(req.qty),
            "OVRS_ORD_UNPR": "0",
        }
        resp = await self._transport("POST", f"{self._base}{_CANCEL_PATH}", headers, body)
        ok = str(resp.get("rt_cd")) == "0"
        return OrderAck(
            client_order_id=req.client_order_id,
            accepted=ok,
            broker_order_no=broker_order_no,
            tr_id=tr_id,
            reject_reason=None if ok else str(resp.get("msg1", "rejected")),
        )

    async def get_balance(self) -> AccountBalance:
        tr_id = self._router.balance_tr_id(self._default_market, self._mode)
        headers = await self._headers(tr_id)
        params = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._creds.account_product_code,
            "OVRS_EXCG_CD": self._default_market.value,
            "TR_CRCY_CD": self._default_market.currency.value,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        resp = await self._transport("GET", f"{self._base}{_BALANCE_PATH}", headers, params)
        return self._parse_balance(resp)

    async def get_open_orders(self) -> list[OrderAck]:
        tr_id = self._router.balance_tr_id(self._default_market, self._mode)  # placeholder group
        headers = await self._headers(tr_id)
        params = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._creds.account_product_code,
            "OVRS_EXCG_CD": self._default_market.value,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        resp = await self._transport("GET", f"{self._base}{_NCCS_PATH}", headers, params)
        return [
            OrderAck(client_order_id="", accepted=True, broker_order_no=str(row.get("odno", "")))
            for row in resp.get("output", [])
        ]

    # -- request building / parsing (pure, testable) ---------------------

    @property
    def _cano(self) -> str:
        return self._creds.account_no.split("-")[0]

    def build_order_body(self, req: OrderRequest) -> dict[str, Any]:
        return {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._creds.account_product_code,
            "OVRS_EXCG_CD": self._router.trading_exchange_code(req.market),
            "PDNO": req.ticker,
            "ORD_QTY": str(req.qty),
            "OVRS_ORD_UNPR": str(req.price),
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",  # limit; overseas market-order handling refined in P9
        }

    @staticmethod
    def _parse_balance(resp: dict[str, Any]) -> AccountBalance:
        positions: list[BalancePosition] = []
        for row in resp.get("output1", []):
            qty = Decimal(str(row.get("ovrs_cblc_qty", "0")))
            if qty == 0:
                continue
            try:
                ccy = Currency(row.get("tr_crcy_cd", "USD"))
            except ValueError:
                ccy = Currency.USD
            positions.append(
                BalancePosition(
                    ticker=str(row.get("ovrs_pdno", "")),
                    market=Market(row.get("ovrs_excg_cd", "NASD")),
                    qty=qty,
                    avg_price=Decimal(str(row.get("pchs_avg_pric", "0"))),
                    currency=ccy,
                )
            )
        cash: dict[Currency, Decimal] = {}
        output2 = resp.get("output2") or {}
        foreign = output2.get("frcr_dncl_amt1") or output2.get("frcr_dncl_amt")
        if foreign is not None and positions:
            cash[positions[0].currency] = Decimal(str(foreign))
        return AccountBalance(cash=cash, positions=positions)

    async def _headers(self, tr_id: str) -> dict[str, str]:
        token = await self._auth.access_token()
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self._creds.app_key,
            "appsecret": self._creds.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    async def _default_transport(
        self, method: str, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers, params=payload)
            else:
                resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data
