"""주문 라이프사이클 회귀 테스트 (#6 #11 #15 #16 #17)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from short_trading_bot.domain.enums import Market, Mode, OrderState, Side
from short_trading_bot.execution.broker.base import BrokerAdapter
from short_trading_bot.execution.broker.kis import KisApiError, KisBrokerAdapter
from short_trading_bot.execution.fill_poller import FillPoller
from short_trading_bot.execution.order_manager import OrderManager
from short_trading_bot.execution.types import (
    AccountBalance,
    Execution,
    Fill,
    OrderAck,
    OrderRecord,
    OrderRequest,
)
from short_trading_bot.execution.unknown_resolver import UnknownOrderResolver
from short_trading_bot.infra.config import KisEnvCreds
from short_trading_bot.infra.kis_auth import KisAuth
from short_trading_bot.persistence.db import session_scope
from short_trading_bot.persistence.models import Order, Position


class _Broker(BrokerAdapter):
    def __init__(self, *, submit_exc: Exception | None = None) -> None:
        self.submit_exc = submit_exc
        self.executions: list[Execution] = []
        self.daily: list[OrderRecord] = []

    @property
    def name(self) -> str:
        return "t"

    async def submit_order(self, req: OrderRequest) -> OrderAck:
        if self.submit_exc is not None:
            raise self.submit_exc
        return OrderAck(req.client_order_id, True, broker_order_no="0001")

    async def cancel_order(self, req: OrderRequest, broker_order_no: str | None) -> OrderAck:
        return OrderAck(req.client_order_id, True)

    async def get_balance(self) -> AccountBalance:
        return AccountBalance(cash={})

    async def get_open_orders(self) -> list[OrderAck]:
        return []

    async def get_executions(self) -> list[Execution]:
        return self.executions

    async def get_daily_orders(self) -> list[OrderRecord]:
        return self.daily


def _req(cid: str = "c1", side: Side = Side.SELL) -> OrderRequest:
    return OrderRequest(
        client_order_id=cid, lot_id="lot1", ticker="005930", market=Market.KRX,
        side=side, qty=Decimal(10), price=Decimal(70000), ord_dvsn="00",
    )


async def _seed_position(sf) -> None:
    async with session_scope(sf) as s:
        s.add(Position(
            lot_id="lot1", ticker="005930", market="KRX", currency="KRW", side="BUY",
            state="HOLDING", strategy_id="s", params_json={}, qty_filled=Decimal(10),
        ))


async def _seed_order(sf, cid: str, state: str, created: datetime, broker_no: str | None = None) -> None:
    async with session_scope(sf) as s:
        s.add(Order(
            order_id=f"o-{cid}", lot_id="lot1", client_order_id=cid, broker_order_no=broker_no,
            side="SELL", qty=Decimal(10), price=Decimal(70000), state=state, created_at=created,
        ))


async def _state(sf, cid: str) -> str:
    async with session_scope(sf) as s:
        return (await s.execute(select(Order.state).where(Order.client_order_id == cid))).scalar_one()


# --- #11 명시적 거부는 UNKNOWN 이 아니라 REJECTED -------------------------------------


def test_kis_error_definitive_classification() -> None:
    url = "https://x/uapi/domestic-stock/v1/trading/order-cash"
    throttle = KisApiError(500, url, '{"rt_cd":"1","msg_cd":"EGW00201","msg1":"초당 거래건수를 초과"}')
    assert throttle.is_definitive_rejection
    assert KisApiError(403, url, "forbidden").is_definitive_rejection
    assert not KisApiError(500, url, "<html>bad gateway</html>").is_definitive_rejection
    assert not KisApiError(502, url, '{"rt_cd":"1","msg_cd":"OPSQ0001"}').is_definitive_rejection


async def test_definitive_rejection_recorded_as_rejected(sf) -> None:
    await _seed_position(sf)
    exc = KisApiError(500, "https://x/uapi/o", '{"rt_cd":"1","msg_cd":"EGW00201","msg1":"초과"}')
    om = OrderManager(_Broker(submit_exc=exc), sf)
    ack = await om.submit(_req())
    assert not ack.accepted and "EGW00201" in (ack.reject_reason or "")
    assert await _state(sf, "c1") == OrderState.REJECTED.value


async def test_ambiguous_error_still_unknown(sf) -> None:
    await _seed_position(sf)
    om = OrderManager(_Broker(submit_exc=TimeoutError("read timeout")), sf)
    try:
        await om.submit(_req())
    except TimeoutError:
        pass
    assert await _state(sf, "c1") == OrderState.UNKNOWN.value


# --- #6 전일 주문 만료 · PENDING_NEW 복구 ---------------------------------------------


async def test_expire_stale_only_touches_previous_days(sf) -> None:
    await _seed_position(sf)
    now = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)  # KST 10:00
    yesterday = now - timedelta(days=1)
    await _seed_order(sf, "old-new", OrderState.NEW.value, yesterday)
    await _seed_order(sf, "old-partial", OrderState.PARTIALLY_FILLED.value, yesterday)
    await _seed_order(sf, "old-unknown", OrderState.UNKNOWN.value, yesterday)
    await _seed_order(sf, "today", OrderState.NEW.value, now - timedelta(minutes=5))
    await _seed_order(sf, "filled", OrderState.FILLED.value, yesterday)

    assert await OrderManager(_Broker(), sf).expire_stale(now) == 3

    for cid in ("old-new", "old-partial", "old-unknown"):
        assert await _state(sf, cid) == OrderState.EXPIRED.value
    assert await _state(sf, "today") == OrderState.NEW.value
    assert await _state(sf, "filled") == OrderState.FILLED.value


async def test_expire_uses_kst_day_boundary(sf) -> None:
    """UTC 15:30 = KST 00:30 다음 날 — UTC 날짜로 판정하면 오늘 주문을 만료시키거나 놓친다."""
    await _seed_position(sf)
    placed = datetime(2026, 9, 24, 6, 0, tzinfo=UTC)  # KST 9/24 15:00
    await _seed_order(sf, "c", OrderState.NEW.value, placed)
    om = OrderManager(_Broker(), sf)
    assert await om.expire_stale(datetime(2026, 9, 24, 14, 0, tzinfo=UTC)) == 0  # KST 9/24 23:00
    assert await om.expire_stale(datetime(2026, 9, 24, 15, 30, tzinfo=UTC)) == 1  # KST 9/25 00:30


async def test_pending_new_at_restart_becomes_unknown(sf) -> None:
    await _seed_position(sf)
    await _seed_order(sf, "c", OrderState.PENDING_NEW.value, datetime.now(UTC))
    assert await OrderManager(_Broker(), sf).orphan_pending_to_unknown() == 1
    assert await _state(sf, "c") == OrderState.UNKNOWN.value


# --- #15 주문번호는 거래일 단위 ----------------------------------------------------------


async def test_fill_poller_ignores_same_order_no_from_previous_day(sf) -> None:
    await _seed_position(sf)
    await _seed_order(sf, "old", OrderState.FILLED.value, datetime.now(UTC) - timedelta(days=1), "0001")
    await _seed_order(sf, "new", OrderState.NEW.value, datetime.now(UTC), "0001")
    broker = _Broker()
    broker.executions = [
        Execution(
            exec_id="0001:4", broker_order_no="0001", ticker="005930", side=Side.SELL,
            qty=Decimal(4), price=Decimal(70000),
        )
    ]
    got: list[Fill] = []

    async def handler(fill: Fill) -> None:
        got.append(fill)

    assert await FillPoller(broker, sf, handler).poll_once() == 1  # MultipleResultsFound 없이
    assert [f.client_order_id for f in got] == ["new"]


async def test_resolver_not_blinded_by_previous_day_order_no(sf) -> None:
    """어제 주문이 같은 번호를 썼어도 오늘 UNKNOWN 주문은 그 번호를 채택해야 한다."""
    await _seed_position(sf)
    await _seed_order(sf, "old", OrderState.FILLED.value, datetime.now(UTC) - timedelta(days=1), "0007")
    await _seed_order(sf, "u", OrderState.UNKNOWN.value, datetime.now(UTC) - timedelta(minutes=10))
    broker = _Broker()
    broker.daily = [OrderRecord(broker_order_no="0007", ticker="005930", side=Side.SELL, qty=Decimal(10))]
    assert await UnknownOrderResolver(broker, sf).poll_once() == 1
    assert await _state(sf, "u") == OrderState.NEW.value  # 만료(→이중 주문) 아님


# --- #16 연속조회 · KST 조회일 / #17 D+2 예수금 -------------------------------------------


def _kis(transport: Any) -> KisBrokerAdapter:
    creds = KisEnvCreds(app_key="ak", app_secret="as", account_no="12345678-01")

    async def fetch() -> tuple[str, int]:
        return "tok", 86400

    base = "https://openapivts.koreainvestment.com:29443"
    return KisBrokerAdapter(
        KisAuth(creds, base, token_fetcher=fetch), creds, base, Mode.PAPER, transport=transport
    )


class _PagedTransport:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[tuple[dict[str, str], dict[str, Any]]] = []

    async def __call__(
        self, method: str, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((dict(headers), dict(payload)))
        return self.pages[len(self.calls) - 1]


def _row(odno: str) -> dict[str, str]:
    return {"odno": odno, "pdno": "005930", "sll_buy_dvsn_cd": "02", "ord_qty": "1", "tot_ccld_qty": "1", "avg_prvs": "100"}


async def test_daily_orders_follow_continuation_pages() -> None:
    transport = _PagedTransport([
        {"output1": [_row("1"), _row("2")], "__tr_cont__": "M", "ctx_area_fk100": "F1", "ctx_area_nk100": "N1"},
        {"output1": [_row("3")], "__tr_cont__": "D", "ctx_area_fk100": "F2", "ctx_area_nk100": ""},
    ])
    orders = await _kis(transport).get_daily_orders()
    assert [o.broker_order_no for o in orders] == ["1", "2", "3"]
    first, second = transport.calls
    assert "tr_cont" not in first[0] and first[1]["CTX_AREA_NK100"] == ""
    assert second[0]["tr_cont"] == "N" and second[1]["CTX_AREA_FK100"] == "F1"
    assert second[1]["CTX_AREA_NK100"] == "N1"


async def test_daily_orders_stop_on_repeated_cursor() -> None:
    page = {"output1": [_row("1")], "__tr_cont__": "M", "ctx_area_fk100": "F", "ctx_area_nk100": "N"}
    transport = _PagedTransport([page, page, page])
    await _kis(transport).get_executions()
    assert len(transport.calls) == 2  # 같은 커서가 반복되면 중단 (무한 루프 방지)


async def test_daily_ccld_uses_kst_date(monkeypatch) -> None:
    import short_trading_bot.execution.broker.kis as kis_mod

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return datetime(2026, 9, 24, 23, 30, tzinfo=UTC).astimezone(tz)  # KST 9/25 08:30

    monkeypatch.setattr(kis_mod, "datetime", _FixedDatetime)
    transport = _PagedTransport([{"output1": []}])
    await _kis(transport).get_executions()
    assert transport.calls[0][1]["INQR_STRT_DT"] == "20260925"


async def test_balance_prefers_settled_d2_cash() -> None:
    async def transport(*_: Any) -> dict[str, Any]:
        return {"output1": [], "output2": [{"dnca_tot_amt": "10000000", "prvs_rcdl_excc_amt": "7000000"}]}

    bal = await _kis(transport).get_balance()
    assert bal.cash[next(iter(bal.cash))] == Decimal("7000000")
