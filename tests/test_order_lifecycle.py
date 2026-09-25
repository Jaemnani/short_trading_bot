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
    # 타임아웃·재시도류 4xx 는 주문이 이미 닿았을 수 있다 → UNKNOWN 유지 (중복 주문 방지)
    for status in (408, 409, 425, 429):
        assert not KisApiError(status, url, "").is_definitive_rejection
    assert KisApiError(400, url, "").is_definitive_rejection


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


async def test_overseas_order_survives_kst_midnight(sf) -> None:
    """미국 DAY 주문은 KST 자정 뒤에도 살아 있다 — KST 날짜로 만료시키면 중복 주문 (#6)."""
    from datetime import UTC as _UTC

    async with session_scope(sf) as s:
        s.add(Position(
            lot_id="us1", ticker="AAPL", market="NASD", currency="USD", side="BUY",
            state="WATCHING", strategy_id="s", params_json={},
        ))
        s.add(Order(
            order_id="o-us", lot_id="us1", client_order_id="us", broker_order_no="9001",
            side="BUY", qty=Decimal(1), price=Decimal(200), state=OrderState.NEW.value,
            created_at=datetime(2026, 9, 24, 14, 30, tzinfo=_UTC),  # KST 9/24 23:30
        ))
    om = OrderManager(_Broker(), sf)
    assert await om.expire_stale(datetime(2026, 9, 24, 19, 0, tzinfo=_UTC)) == 0  # KST 9/25 04:00
    assert await _state(sf, "us") == OrderState.NEW.value
    assert await om.expire_stale(datetime(2026, 9, 25, 15, 0, tzinfo=_UTC)) == 1  # 24h 초과


async def test_resolver_readopts_presumed_expired_order_when_it_appears(sf) -> None:
    """'주문내역에 없음'으로 추정 만료(REJECTED)된 주문이 늦게 나타나면 채택 → 체결 매칭 가능."""
    await _seed_position(sf)
    await _seed_order(sf, "u", OrderState.UNKNOWN.value, datetime.now(UTC) - timedelta(minutes=10))
    broker = _Broker()
    resolver = UnknownOrderResolver(broker, sf)
    assert await resolver.poll_once() == 1
    assert await _state(sf, "u") == OrderState.REJECTED.value  # 추정 만료

    broker.daily = [OrderRecord(broker_order_no="0042", ticker="005930", side=Side.SELL, qty=Decimal(10))]
    assert await resolver.poll_once() == 1
    async with session_scope(sf) as s:
        row = (await s.execute(select(Order).where(Order.client_order_id == "u"))).scalar_one()
    assert row.state == OrderState.NEW.value and row.broker_order_no == "0042"


async def test_prior_day_fills_recovered_before_expiry(sf) -> None:
    """엔진이 꺼진 채 날짜를 넘김: 전일 체결을 먼저 반영하고 나서 만료 (보유 유실·중복 진입 방지)."""
    from short_trading_bot.app.service import TradingService
    from short_trading_bot.risk.limits import RiskLimits
    from short_trading_bot.risk.manager import RiskManager

    await _seed_position(sf)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    await _seed_order(sf, "prev", OrderState.NEW.value, yesterday, broker_no="0009")

    class _Prior(_Broker):
        def __init__(self) -> None:
            super().__init__()
            self.asked: list[Any] = []

        async def get_executions_on(self, day: Any) -> list[Execution]:
            self.asked.append(day)
            return [Execution(
                exec_id="0009:10", broker_order_no="0009", ticker="005930", side=Side.SELL,
                qty=Decimal(10), price=Decimal(70000),
            )]

    broker = _Prior()
    svc = TradingService(broker, sf, RiskManager(RiskLimits()), {})
    await svc.expire_stale_orders(db_only=True)
    assert broker.asked  # 그날 체결을 조회했다
    assert await _state(sf, "prev") == OrderState.FILLED.value  # 만료가 아니라 체결 반영
    async with session_scope(sf) as s:
        pos = await s.get(Position, "lot1")
    assert pos is not None and pos.qty_filled == 0  # 보유 10주 매도 체결 반영


async def test_prior_day_order_kept_when_fill_lookup_fails(sf) -> None:
    from short_trading_bot.app.service import TradingService
    from short_trading_bot.risk.limits import RiskLimits
    from short_trading_bot.risk.manager import RiskManager

    await _seed_position(sf)
    await _seed_order(sf, "prev", OrderState.NEW.value, datetime.now(UTC) - timedelta(days=1), broker_no="0009")

    class _Down(_Broker):
        async def get_executions_on(self, day: Any) -> list[Execution]:
            raise RuntimeError("KIS 500")

    svc = TradingService(_Down(), sf, RiskManager(RiskLimits()), {})
    await svc.expire_stale_orders(db_only=True)
    assert await _state(sf, "prev") == OrderState.NEW.value  # 확인 전엔 만료 안 함


async def test_adopted_order_relocks_service_immediately(sf) -> None:
    """(재)채택된 주문은 체결 전이라도 즉시 서비스 잠금·메타가 걸린다 (중복 주문·조기 flat 방지)."""
    from short_trading_bot.app.service import TradingService
    from short_trading_bot.domain.factory import PositionFactory
    from short_trading_bot.domain.signal import Signal
    from short_trading_bot.risk.limits import RiskLimits
    from short_trading_bot.risk.manager import RiskManager
    from short_trading_bot.strategy.templates import StrategyTemplate

    lot = PositionFactory.create(Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1"))
    async with session_scope(sf) as s:
        s.add(Position(
            lot_id=lot.lot_id, ticker="005930", market="KRX", currency="KRW", side="BUY",
            state="WATCHING", strategy_id="trend_long_v1", params_json=lot.params.model_dump(mode="json"),
        ))
        s.add(Order(
            order_id="o-u", lot_id=lot.lot_id, client_order_id="u", side="BUY", qty=Decimal(10),
            price=Decimal(70000), state=OrderState.UNKNOWN.value,
            created_at=datetime.now(UTC) - timedelta(minutes=10),
        ))
    broker = _Broker()
    broker.daily = [OrderRecord(broker_order_no="0077", ticker="005930", side=Side.BUY, qty=Decimal(10))]
    svc = TradingService(broker, sf, RiskManager(RiskLimits()), {})
    assert await svc.make_unknown_resolver().poll_once() == 1
    assert svc._pending[(lot.lot_id, Side.BUY)] == "u" and "u" in svc._co_map
    assert not svc.is_flat()


async def test_prior_day_recovery_uses_fresh_dedup_per_day(sf) -> None:
    """두 날의 체결 ID(주문번호:누적수량)가 같아도 각각 반영된다 (날짜별 새 폴러)."""
    from short_trading_bot.app.service import TradingService
    from short_trading_bot.risk.limits import RiskLimits
    from short_trading_bot.risk.manager import RiskManager

    await _seed_position(sf)
    d2 = datetime.now(UTC) - timedelta(days=2)
    d1 = datetime.now(UTC) - timedelta(days=1)
    await _seed_order(sf, "a", OrderState.NEW.value, d2, broker_no="0001")
    async with session_scope(sf) as s:
        s.add(Order(
            order_id="o-b", lot_id="lot1", client_order_id="b", broker_order_no="0001", side="SELL",
            qty=Decimal(5), price=Decimal(70000), state=OrderState.NEW.value, created_at=d1,
        ))

    class _TwoDays(_Broker):
        async def get_executions_on(self, day: Any) -> list[Execution]:
            return [Execution(  # 두 날 모두 같은 exec_id "0001:5"
                exec_id="0001:5", broker_order_no="0001", ticker="005930", side=Side.SELL,
                qty=Decimal(5), price=Decimal(70000),
            )]

    svc = TradingService(_TwoDays(), sf, RiskManager(RiskLimits()), {})
    await svc.expire_stale_orders(db_only=True)
    async with session_scope(sf) as s:
        pos = await s.get(Position, "lot1")
    assert pos is not None and pos.qty_filled == 0  # 10주 = 5 + 5 둘 다 반영
    assert await _state(sf, "b") == OrderState.FILLED.value
