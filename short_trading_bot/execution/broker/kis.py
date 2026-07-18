"""KIS domestic-stock (국내주식) broker adapter (REST).

Mirrors the overseas adapter: injectable async transport makes request-building and
response-parsing unit-testable without network. Real fills arrive via the WebSocket
(H0STCNI0/H0STCNI9) or polling, wired by the runtime.

TR_IDs verified vs the KIS official sample repo (next-gen KRX/NXT): buy TTTC0012U,
sell TTTC0011U, cancel TTTC0013U; bodies carry EXCG_ID_DVSN_CD (KRX/NXT/SOR), sell adds
SLL_TYPE. ⚠️ Response field names still follow common KIS shapes — confirm on the portal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from ...domain.enums import Currency, Market, Mode, Side
from ...infra.config import KisEnvCreds
from ...infra.kis_auth import KisAuth
from ..fees import KRX_FEES, FeeModel
from ..types import AccountBalance, BalancePosition, Execution, OrderAck, OrderRequest
from .base import BrokerAdapter
from .market_router import MarketRouter

Transport = Callable[[str, str, dict[str, str], dict[str, Any]], Awaitable[dict[str, Any]]]

_ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
_CANCEL_PATH = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
_BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
_CCLD_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
_CCLD_TR_LIVE = "TTTC0081R"  # 일별주문체결조회 — ⚠️ verify on portal


class KisBrokerAdapter(BrokerAdapter):
    def __init__(
        self,
        auth: KisAuth,
        creds: KisEnvCreds,
        base_url: str,
        mode: Mode,
        router: MarketRouter | None = None,
        *,
        transport: Transport | None = None,
        timeout: float = 10.0,
        fees: FeeModel | None = None,
    ) -> None:
        self._auth = auth
        self._creds = creds
        self._base = base_url.rstrip("/")
        self._mode = mode
        self._router = router or MarketRouter()
        self._transport = transport or self._default_transport
        self._timeout = timeout
        self._fees = fees or KRX_FEES

    @property
    def name(self) -> str:
        return "kis"

    async def submit_order(self, req: OrderRequest) -> OrderAck:
        tr_id = self._router.order_tr_id(Market.KRX, req.side, self._mode)
        headers = await self._headers(tr_id)
        resp = await self._transport("POST", f"{self._base}{_ORDER_PATH}", headers, self.build_order_body(req))
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
        tr_id = self._router.cancel_tr_id(Market.KRX, self._mode)
        headers = await self._headers(tr_id)
        body = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._creds.account_product_code,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": broker_order_no or "",
            "ORD_DVSN": req.ord_dvsn,
            "RVSE_CNCL_DVSN_CD": "02",  # 02 = cancel, 01 = revise
            "ORD_QTY": str(req.qty),
            "ORD_UNPR": str(req.price),
            "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": "KRX",
        }
        resp = await self._transport("POST", f"{self._base}{_CANCEL_PATH}", headers, body)
        return OrderAck(
            client_order_id=req.client_order_id,
            accepted=str(resp.get("rt_cd")) == "0",
            broker_order_no=broker_order_no,
            tr_id=tr_id,
            reject_reason=None if str(resp.get("rt_cd")) == "0" else str(resp.get("msg1", "rejected")),
        )

    async def get_balance(self) -> AccountBalance:
        tr_id = self._router.balance_tr_id(Market.KRX, self._mode)
        headers = await self._headers(tr_id)
        params = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._creds.account_product_code,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        resp = await self._transport("GET", f"{self._base}{_BALANCE_PATH}", headers, params)
        return self._parse_balance(resp)

    async def get_open_orders(self) -> list[OrderAck]:
        return []  # 미체결조회 (inquire-psbl-rvsecncl) wired with the live feed in deployment

    async def get_executions(self) -> list[Execution]:
        tr_id = ("V" + _CCLD_TR_LIVE[1:]) if self._mode is Mode.PAPER else _CCLD_TR_LIVE
        headers = await self._headers(tr_id)
        today = datetime.now(UTC).strftime("%Y%m%d")
        params = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._creds.account_product_code,
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "01",  # 01 = 체결
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        resp = await self._transport("GET", f"{self._base}{_CCLD_PATH}", headers, params)
        return self._parse_executions(resp)

    def _parse_executions(self, resp: dict[str, Any]) -> list[Execution]:
        out: list[Execution] = []
        for row in resp.get("output1", []):
            qty = Decimal(str(row.get("tot_ccld_qty", "0") or "0"))
            if qty == 0:
                continue
            side = Side.SELL if str(row.get("sll_buy_dvsn_cd", "")) == "01" else Side.BUY
            price = Decimal(str(row.get("avg_prvs", "0") or "0"))
            # 응답에 수수료·제세금 필드가 없어 요율로 추정한다 (누적 체결금액 기준이라
            # FillPoller의 누적-델타 회계와 그대로 맞물린다).
            notional = qty * price
            out.append(
                Execution(
                    exec_id=str(row.get("odno", "")) + ":" + str(row.get("tot_ccld_qty", "")),
                    broker_order_no=str(row.get("odno", "")),
                    ticker=str(row.get("pdno", "")),
                    side=side,
                    qty=qty,
                    price=price,
                    fee=self._fees.fee(notional),
                    tax=self._fees.tax(side, notional),
                    currency=Currency.KRW,
                )
            )
        return out

    # -- request building / parsing (pure, testable) ---------------------

    @property
    def _cano(self) -> str:
        return self._creds.account_no.split("-")[0]

    def build_order_body(self, req: OrderRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._creds.account_product_code,
            "PDNO": req.ticker,
            "ORD_DVSN": req.ord_dvsn,  # 00 limit, 01 market
            "ORD_QTY": str(req.qty),
            "ORD_UNPR": str(req.price),  # "0" for market
            "EXCG_ID_DVSN_CD": "KRX",  # next-gen routing: KRX | NXT | SOR
        }
        if req.side is Side.SELL:
            body["SLL_TYPE"] = "01"  # normal sell
        return body

    @staticmethod
    def _parse_balance(resp: dict[str, Any]) -> AccountBalance:
        positions: list[BalancePosition] = []
        for row in resp.get("output1", []):
            qty = Decimal(str(row.get("hldg_qty", "0")))
            if qty == 0:
                continue
            positions.append(
                BalancePosition(
                    ticker=str(row.get("pdno", "")),
                    market=Market.KRX,
                    qty=qty,
                    avg_price=Decimal(str(row.get("pchs_avg_pric", "0"))),
                    currency=Currency.KRW,
                )
            )
        cash: dict[Currency, Decimal] = {}
        output2 = resp.get("output2") or []
        summary = output2[0] if isinstance(output2, list) and output2 else output2
        if isinstance(summary, dict) and "dnca_tot_amt" in summary:
            cash[Currency.KRW] = Decimal(str(summary["dnca_tot_amt"]))
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
