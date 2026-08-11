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

from ...domain.enums import Currency, Market, Mode, Side
from ...infra.config import KisEnvCreds
from ...infra.http import shared_client
from ...infra.kis_auth import KisAuth
from ...infra.rate_limit import shared_limiter
from ..fees import KRX_FEES, FeeModel
from ..types import (
    AccountBalance,
    BalancePosition,
    Execution,
    OrderAck,
    OrderRecord,
    OrderRequest,
)
from .base import BrokerAdapter
from .market_router import MarketRouter

Transport = Callable[[str, str, dict[str, str], dict[str, Any]], Awaitable[dict[str, Any]]]


class KisApiError(RuntimeError):
    """KIS REST 실패 — 상태코드와 본문(msg_cd/msg1)을 보존한다.

    본문이 없으면 '왜 500 인가'(초당 한도 초과 / TR 미지원 / 서버 장애)를 구분할 수 없다."""

    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = body
        path = url.split("/uapi")[-1].split("?")[0]
        super().__init__(f"KIS HTTP {status} {path}: {body}")

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
        # KIS 공식 한도: 모의 초당 2건 / 실전 20건. 한도에 딱 맞추면 서버측 계측 오차로
        # 다시 초과하므로 보수적으로 잡는다 (2026-08-11 EGW00201 사고).
        self._rate_limit = 1.5 if mode is Mode.PAPER else 12.0
        self._rate_burst = 2.0 if mode is Mode.PAPER else 15.0

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
        resp = await self._daily_ccld("01")  # 01 = 체결만
        return self._parse_executions(resp)

    async def get_daily_orders(self) -> list[OrderRecord]:
        resp = await self._daily_ccld("00")  # 00 = 전체 (체결 + 미체결)
        out: list[OrderRecord] = []
        for row in resp.get("output1", []):
            odno = str(row.get("odno", ""))
            if not odno:
                continue
            side = Side.SELL if str(row.get("sll_buy_dvsn_cd", "")) == "01" else Side.BUY
            out.append(
                OrderRecord(
                    broker_order_no=odno,
                    ticker=str(row.get("pdno", "")),
                    side=side,
                    qty=Decimal(str(row.get("ord_qty", "0") or "0")),
                )
            )
        return out

    async def _daily_ccld(self, ccld_dvsn: str) -> dict[str, Any]:
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
            "CCLD_DVSN": ccld_dvsn,
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        return await self._transport("GET", f"{self._base}{_CCLD_PATH}", headers, params)

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
        """국내주식 현금주문 body — KIS 공식 샘플(examples_llm/domestic_stock/order_cash)과
        **키 집합을 정확히 일치**시킨다.

        2026-08-11 사고: SLL_TYPE 만 매도에 붙이고 CNDT_PRIC(조건가격)을 통째로 빠뜨렸더니
        매도 주문이 `IGW00007 "MCA 전문바디 구성 중 오류"` 로 거부됐다. 게이트웨이가 고정
        포맷 전문을 조립하는데 필드가 없으면 구성이 깨진다. 매수는 우연히 통과해
        **실주문 전환(08-03) 이후 매수만 되고 매도는 100% 실패** — 손절이 작동하지 않았다.
        (7월의 매도 체결 17건은 시뮬레이터라 이 경로를 안 탔다.)

        → 선택 필드도 빈 문자열로 항상 실어 보낸다. 공식 샘플의 기본값과 동일.
        """
        return {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._creds.account_product_code,
            "PDNO": req.ticker,
            "ORD_DVSN": req.ord_dvsn,  # 00 limit, 01 market
            "ORD_QTY": str(req.qty),
            "ORD_UNPR": str(req.price),  # "0" for market
            "EXCG_ID_DVSN_CD": "KRX",  # next-gen routing: KRX | NXT | SOR
            "SLL_TYPE": "01" if req.side is Side.SELL else "",  # 01=일반매도, 매수는 공란
            "CNDT_PRIC": "",  # 조건가격 (일반주문은 공란) — 누락 시 IGW00007
        }

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
        # 초당 한도 게이트 — 넘기면 KIS 가 500(EGW00201) + Connection: close 로 응답해
        # 커넥션 풀이 깨지고 DNS 폭주 → 시세 연결까지 무너진다 (infra/rate_limit.py).
        await shared_limiter(self._rate_limit, self._rate_burst).acquire()
        # 공용 클라이언트 (커넥션·DNS 재사용). 호출마다 새로 만들면 하루 수만 번의 DNS
        # 조회로 리졸버가 실패하고, 그 예외가 시세 연결까지 끊는다 — infra/http.py 참조.
        client = shared_client(self._timeout)
        if method == "GET":
            resp = await client.get(url, headers=headers, params=payload, timeout=self._timeout)
        else:
            resp = await client.post(url, headers=headers, json=payload, timeout=self._timeout)
        if resp.status_code >= 400:
            # KIS 는 실패 사유를 본문(msg_cd/msg1)에만 담는다. raise_for_status 만 쓰면
            # 로그에 "500" 만 남아 원인(초당 한도 초과인지 서버 장애인지)을 알 수 없다 —
            # 2026-08-10 장중 체결조회 356회 실패의 사유를 끝내 못 밝힌 이유.
            raise KisApiError(resp.status_code, url, resp.text[:300])
        data: dict[str, Any] = resp.json()
        return data
