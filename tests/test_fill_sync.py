"""체결 → 메모리 랏 동기화 회귀 테스트 (#3 #4 #5).

실계좌에서는 지정가가 걸려 있다가 나중에(폴링으로) 체결된다. 페이퍼 브로커는 즉시
체결이라 이 경로가 안 드러나므로, 주문을 접수만 하고 체결은 테스트가 직접 넣는 브로커를 쓴다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from short_trading_bot.app.service import TradingService
from short_trading_bot.domain.enums import OrderState, PositionState, Side
from short_trading_bot.execution.broker.base import BrokerAdapter
from short_trading_bot.execution.types import AccountBalance, Fill, OrderAck, OrderRequest
from short_trading_bot.infra.notifier.base import InMemoryNotifier
from short_trading_bot.persistence.db import session_scope
from short_trading_bot.persistence.models import Order, Position
from short_trading_bot.risk.limits import RiskLimits
from short_trading_bot.risk.manager import RiskManager
from short_trading_bot.strategy.templates import StrategyTemplate


class RestingBroker(BrokerAdapter):
    """접수만 하고 체결은 테스트가 ``fill_handler`` 로 넣는다 (실계좌의 미체결 지정가)."""

    def __init__(self) -> None:
        self.n = 0
        self.cancelled: list[str | None] = []
        self.submitted: list[OrderRequest] = []

    @property
    def name(self) -> str:
        return "resting"

    async def submit_order(self, req: OrderRequest) -> OrderAck:
        self.n += 1
        self.submitted.append(req)
        return OrderAck(req.client_order_id, True, broker_order_no=f"B{self.n}")

    async def cancel_order(self, req: OrderRequest, broker_order_no: str | None) -> OrderAck:
        self.cancelled.append(broker_order_no)
        return OrderAck(req.client_order_id, True, broker_order_no=broker_order_no)

    async def get_balance(self) -> AccountBalance:
        return AccountBalance(cash={})

    async def get_open_orders(self) -> list[OrderAck]:
        return []


_TMPL = StrategyTemplate(strategy_id="trend_long_v1")


def _svc(
    sf: async_sessionmaker[AsyncSession], broker: RestingBroker | None = None
) -> tuple[TradingService, RestingBroker, InMemoryNotifier]:
    broker = broker or RestingBroker()
    notifier = InMemoryNotifier()
    svc = TradingService(broker, sf, RiskManager(RiskLimits()), {}, notifier=notifier)
    return svc, broker, notifier


async def _db_position(sf: async_sessionmaker[AsyncSession], lot_id: str) -> Position:
    async with session_scope(sf) as s:
        row = await s.get(Position, lot_id)
    assert row is not None
    return row


async def _order_state(sf: async_sessionmaker[AsyncSession], cid: str) -> str:
    async with session_scope(sf) as s:
        return (await s.execute(select(Order.state).where(Order.client_order_id == cid))).scalar_one()


async def test_fill_after_restart_reaches_memory_lot(sf) -> None:
    """재시작 전에 낸 매수가 재시작 뒤 체결 → 메모리 랏이 보유를 알아야 한다 (#3)."""
    s1, _, _ = _svc(sf)
    lot = await s1._spawn("005930", _TMPL)
    lot.restore_pending_entry(Decimal("90"), Decimal("10"))  # 진입 intent 가 남긴 손절가/수량
    await s1._submit(lot, Side.BUY, Decimal(10), Decimal(100), is_add=False, reason="enter")
    await s1._sync_runtime(lot)
    cid = s1._pending[(lot.lot_id, Side.BUY)]

    s2, _, _ = _svc(sf)  # 재시작
    await s2.hydrate()
    await s2._on_fill(Fill(client_order_id=cid, qty=Decimal(10), price=Decimal(100)))

    restored = s2.lot("005930")
    assert restored is not None
    assert restored.state is PositionState.HOLDING and restored.qty == Decimal(10)
    assert restored.initial_stop == Decimal("90")  # 진입 손절가도 복원돼 적용
    assert restored.original_qty == Decimal("10")
    assert (lot.lot_id, Side.BUY) not in s2._pending  # 전량 체결 → 잠금 해제


async def test_partial_fill_after_restart_keeps_lock_until_complete(sf) -> None:
    s1, _, _ = _svc(sf)
    lot = await s1._spawn("005930", _TMPL)
    await s1._submit(lot, Side.BUY, Decimal(10), Decimal(100), is_add=False, reason="enter")
    cid = s1._pending[(lot.lot_id, Side.BUY)]
    await s1._on_fill(Fill(client_order_id=cid, qty=Decimal(4), price=Decimal(100)))

    s2, _, _ = _svc(sf)
    await s2.hydrate()
    await s2._on_fill(Fill(client_order_id=cid, qty=Decimal(3), price=Decimal(100)))
    restored = s2.lot("005930")
    assert restored is not None and restored.qty == Decimal(7)
    assert (lot.lot_id, Side.BUY) in s2._pending  # 3주 남음 — 아직 잠금
    await s2._on_fill(Fill(client_order_id=cid, qty=Decimal(3), price=Decimal(100)))
    assert restored.qty == Decimal(10) and (lot.lot_id, Side.BUY) not in s2._pending


async def test_late_fill_of_cancelled_order_reaches_memory_and_stays_cancelled(sf) -> None:
    """손절이 익절 주문을 취소했는데 취소 직전 체결분이 늦게 옴 (#4)."""
    svc, _, _ = _svc(sf)
    lot = await svc._spawn("000660", _TMPL)
    await svc._submit(lot, Side.BUY, Decimal(10), Decimal(100), is_add=False, reason="enter")
    await svc._on_fill(
        Fill(client_order_id=svc._pending[(lot.lot_id, Side.BUY)], qty=Decimal(10), price=Decimal(100))
    )
    await svc._submit(lot, Side.SELL, Decimal(5), Decimal(110), is_add=False, reason="take_profit_1")
    tp = svc._pending[(lot.lot_id, Side.SELL)]
    assert await svc._submit(
        lot, Side.SELL, lot.qty, Decimal(90), is_add=False, reason="stop", replace_pending=True
    )
    stop_cid = svc._pending[(lot.lot_id, Side.SELL)]

    await svc._on_fill(Fill(client_order_id=tp, qty=Decimal(5), price=Decimal(110)))

    assert lot.qty == Decimal(5)  # 메모리도 5주 — 다음 손절은 실제 보유 수량으로
    assert (await _db_position(sf, lot.lot_id)).qty_filled == Decimal(5)
    assert await _order_state(sf, tp) == OrderState.CANCELLED.value  # 되살아나지 않음
    assert svc._pending[(lot.lot_id, Side.SELL)] == stop_cid  # 손절 주문 잠금은 그대로


async def test_exit_cancels_open_entry_buy(sf) -> None:
    """부분체결 뒤 손절: 걸려 있는 매수 잔량을 먼저 취소 (#5)."""
    svc, broker, _ = _svc(sf)
    lot = await svc._spawn("035420", _TMPL)
    await svc._submit(lot, Side.BUY, Decimal(100), Decimal(100), is_add=False, reason="enter")
    buy = svc._pending[(lot.lot_id, Side.BUY)]
    await svc._on_fill(Fill(client_order_id=buy, qty=Decimal(30), price=Decimal(100)))

    await svc._cancel_open_buy(lot, reason="exit_cancels_entry")

    assert broker.cancelled == ["B1"]
    assert (lot.lot_id, Side.BUY) not in svc._pending
    assert await _order_state(sf, buy) == OrderState.CANCELLED.value


async def test_buy_fill_after_close_reopens_lot_under_management(sf) -> None:
    """청산(CLOSED) 뒤 매수 잔량이 체결되면 고아 주식이 아니라 다시 관리 대상 (#5)."""
    svc, _, notifier = _svc(sf)
    lot = await svc._spawn("035420", _TMPL)
    await svc._submit(lot, Side.BUY, Decimal(100), Decimal(100), is_add=False, reason="enter")
    buy = svc._pending[(lot.lot_id, Side.BUY)]
    await svc._on_fill(Fill(client_order_id=buy, qty=Decimal(30), price=Decimal(100)))
    await svc._submit(lot, Side.SELL, lot.qty, Decimal(95), is_add=False, reason="hard_stop")
    sell = svc._pending[(lot.lot_id, Side.SELL)]
    await svc._on_fill(Fill(client_order_id=sell, qty=Decimal(30), price=Decimal(95)))
    assert lot.state is PositionState.CLOSED
    # 슬롯에 새 관망 랏이 스폰된 상황까지 재현
    fresh = await svc._spawn("035420", _TMPL)

    await svc._on_fill(Fill(client_order_id=buy, qty=Decimal(70), price=Decimal(100)))

    assert lot.state is PositionState.HOLDING and lot.qty == Decimal(70) and lot.is_open
    assert svc.lot("035420") is lot  # 관리 슬롯으로 복귀
    assert fresh.state is PositionState.CANCELLED
    db = await _db_position(sf, lot.lot_id)
    assert db.state == PositionState.HOLDING.value and db.qty_filled == Decimal(70)
    assert any(n.event == "fill.orphan_reopened" for n in notifier.sent)


async def test_kill_switch_cancels_entries_and_replaces_resting_sell(sf) -> None:
    """flat-all: 매수 대기 취소 + 걸린 익절 매도를 취소하고 청산 매도로 교체 (#7)."""
    svc, broker, _ = _svc(sf)
    held = await svc._spawn("005930", _TMPL)
    await svc._submit(held, Side.BUY, Decimal(10), Decimal(100), is_add=False, reason="enter")
    await svc._on_fill(
        Fill(client_order_id=svc._pending[(held.lot_id, Side.BUY)], qty=Decimal(10), price=Decimal(100))
    )
    await svc._submit(held, Side.SELL, Decimal(5), Decimal(120), is_add=False, reason="take_profit_1")
    tp = svc._pending[(held.lot_id, Side.SELL)]
    waiting = await svc._spawn("000660", _TMPL)
    await svc._submit(waiting, Side.BUY, Decimal(3), Decimal(50), is_add=False, reason="enter")
    svc._last_price["005930"] = Decimal(9800)

    await svc._flat_all()

    assert (waiting.lot_id, Side.BUY) not in svc._pending  # 진입 대기 취소
    flat = svc._pending[(held.lot_id, Side.SELL)]
    assert flat != tp and await _order_state(sf, tp) == OrderState.CANCELLED.value
    assert broker.submitted[-1].qty == Decimal(10) and broker.submitted[-1].price == Decimal(9800)
    n = len(broker.submitted)
    await svc._flat_all()  # 같은 가격이면 재주문하지 않음
    assert len(broker.submitted) == n
    svc._last_price["005930"] = Decimal(9500)
    await svc._flat_all()  # 시세가 바뀌면 재가격
    assert broker.submitted[-1].price == Decimal(9500)


# --- Codex 크로스리뷰 1회차 지적 ------------------------------------------------------


async def test_flat_all_request_kept_while_buy_is_unresolved(sf) -> None:
    """보유 0 이어도 UNKNOWN 매수가 남아 있으면 청산 요청을 해제하지 않는다."""
    from short_trading_bot.domain.enums import Resolution
    from short_trading_bot.market.types import Bar

    svc, _, _ = _svc(sf)
    lot = await svc._spawn("005930", _TMPL)
    await svc._submit(lot, Side.BUY, Decimal(10), Decimal(100), is_add=False, reason="enter")
    cid = svc._pending[(lot.lot_id, Side.BUY)]
    async with session_scope(sf) as s:  # 제출 타임아웃으로 UNKNOWN 이 된 상황
        (await s.execute(select(Order).where(Order.client_order_id == cid))).scalar_one().state = "UNKNOWN"
    svc.control.stop()
    bar = Bar("005930", Resolution.D1, datetime(2026, 9, 25, tzinfo=UTC), *(Decimal(100),) * 4,
              Decimal(1), Decimal(100))

    await svc.process(bar)
    assert svc.control.flat_all_requested  # UNKNOWN 은 취소 불가 → 아직 평탄 아님

    async with session_scope(sf) as s:  # resolver 가 '브로커에 없음'으로 거부 확정
        (await s.execute(select(Order).where(Order.client_order_id == cid))).scalar_one().state = "REJECTED"
    await svc.process(bar)
    assert not svc.control.flat_all_requested and svc.is_flat()


async def test_replacement_sell_uses_quantity_left_after_unseen_partial_fill(sf) -> None:
    """교체 직전, 취소된 매도의 미반영 부분체결을 반영하고 남은 수량만 다시 낸다."""
    from short_trading_bot.execution.types import Execution

    class _Exec(RestingBroker):
        def __init__(self) -> None:
            super().__init__()
            self.executions: list[Execution] = []

        async def get_executions(self) -> list[Execution]:
            return self.executions

    broker = _Exec()
    svc, _, _ = _svc(sf, broker)
    lot = await svc._spawn("005930", _TMPL)
    await svc._submit(lot, Side.BUY, Decimal(10), Decimal(9000), is_add=False, reason="enter")
    await svc._on_fill(Fill(svc._pending[(lot.lot_id, Side.BUY)], Decimal(10), Decimal(9000)))
    await svc._submit(lot, Side.SELL, Decimal(10), Decimal(9000), is_add=False, reason="kill_switch")
    # 브로커에선 4주가 이미 체결됐지만 아직 폴링 전
    broker.executions = [Execution(
        exec_id="B2:4", broker_order_no="B2", ticker="005930", side=Side.SELL,
        qty=Decimal(4), price=Decimal(9000),
    )]
    assert await svc._submit(
        lot, Side.SELL, lot.qty, Decimal(8900), is_add=False, reason="kill_switch", replace_pending=True
    )
    assert lot.qty == Decimal(6) and broker.submitted[-1].qty == Decimal(6)


async def test_delayed_buy_for_lot_closed_before_restart_is_rebuilt_and_managed(sf) -> None:
    """재시작 전 CLOSED 랏(hydrate 대상 아님)의 매수 잔량 체결 → 즉시 랏 재구성·관리 편입."""
    s1, _, _ = _svc(sf)
    lot = await s1._spawn("035420", _TMPL)
    await s1._submit(lot, Side.BUY, Decimal(100), Decimal(100), is_add=False, reason="enter")
    buy = s1._pending[(lot.lot_id, Side.BUY)]
    await s1._on_fill(Fill(buy, Decimal(30), Decimal(100)))
    await s1._submit(lot, Side.SELL, Decimal(30), Decimal(95), is_add=False, reason="hard_stop")
    await s1._on_fill(Fill(s1._pending[(lot.lot_id, Side.SELL)], Decimal(30), Decimal(95)))
    assert (await _db_position(sf, lot.lot_id)).state == PositionState.CLOSED.value

    s2, _, notifier = _svc(sf)  # 재시작 — CLOSED 랏은 복원되지 않는다
    await s2.hydrate()
    assert s2.lot("035420") is None
    await s2._on_fill(Fill(buy, Decimal(70), Decimal(100)))

    managed = s2.lot("035420")
    assert managed is not None and managed.lot_id == lot.lot_id
    assert managed.is_open and managed.qty == Decimal(70)
    assert any(n.event == "fill.orphan_reopened" for n in notifier.sent)


async def test_replacement_sell_aborts_when_fill_refresh_fails(sf) -> None:
    """교체 매도: 취소 뒤 체결 반영이 실패하면 낡은 보유량으로 내지 않고, 반영 성공 후에만 낸다."""
    from short_trading_bot.execution.types import Execution

    class _Flaky(RestingBroker):
        def __init__(self) -> None:
            super().__init__()
            self.fail = False
            self.executions: list[Execution] = []

        async def get_executions(self) -> list[Execution]:
            if self.fail:
                raise RuntimeError("KIS HTTP 500")
            return self.executions

    broker = _Flaky()
    svc, _, _ = _svc(sf, broker)
    lot = await svc._spawn("005930", _TMPL)
    await svc._submit(lot, Side.BUY, Decimal(10), Decimal(9000), is_add=False, reason="enter")
    await svc._on_fill(Fill(svc._pending[(lot.lot_id, Side.BUY)], Decimal(10), Decimal(9000)))
    await svc._submit(lot, Side.SELL, Decimal(10), Decimal(9000), is_add=False, reason="kill_switch")
    broker.executions = [Execution(
        exec_id="B2:4", broker_order_no="B2", ticker="005930", side=Side.SELL,
        qty=Decimal(4), price=Decimal(9000),
    )]
    broker.fail = True
    n = len(broker.submitted)

    assert not await svc._submit(
        lot, Side.SELL, lot.qty, Decimal(8900), is_add=False, reason="kill_switch", replace_pending=True
    )
    assert len(broker.submitted) == n  # 낡은 수량(10주)으로 재제출하지 않음
    # 다음 봉: 잠금은 풀렸지만 반영 실패 표시가 남아 있어 여전히 보류
    assert not await svc._submit(lot, Side.SELL, lot.qty, Decimal(8900), is_add=False, reason="kill_switch")
    assert len(broker.submitted) == n

    broker.fail = False  # 반영 성공 → 남은 6주만
    assert await svc._submit(lot, Side.SELL, lot.qty, Decimal(8900), is_add=False, reason="kill_switch")
    assert lot.qty == Decimal(6) and broker.submitted[-1].qty == Decimal(6)


async def test_open_buy_on_closed_lot_survives_restart_and_blocks_flat(sf) -> None:
    """청산으로 CLOSED 됐지만 매수 주문이 살아 있는 랏: 재시작 뒤에도 잠금·메타가 복원돼
    긴급중지 엔진이 조기 종료하지 않고, flat-all 이 그 매수를 취소한다."""
    s1, _, _ = _svc(sf)
    lot = await s1._spawn("035420", _TMPL)
    await s1._submit(lot, Side.BUY, Decimal(100), Decimal(100), is_add=False, reason="enter")
    buy = s1._pending[(lot.lot_id, Side.BUY)]
    await s1._on_fill(Fill(buy, Decimal(30), Decimal(100)))
    await s1._submit(lot, Side.SELL, Decimal(30), Decimal(95), is_add=False, reason="hard_stop")
    await s1._on_fill(Fill(s1._pending[(lot.lot_id, Side.SELL)], Decimal(30), Decimal(95)))
    assert (await _db_position(sf, lot.lot_id)).state == PositionState.CLOSED.value

    s2, broker2, _ = _svc(sf)  # 재시작
    await s2.hydrate()
    assert s2._pending[(lot.lot_id, Side.BUY)] == buy
    assert not s2.is_flat()  # 살아 있는 매수 → 아직 종료하면 안 됨

    await s2._flat_all()
    assert broker2.cancelled  # 슬롯 밖 랏의 매수도 취소
    assert (lot.lot_id, Side.BUY) not in s2._pending and s2.is_flat()


class _ExecBroker(RestingBroker):
    """체결내역(폴링 원천)을 테스트가 주입 — 실패도 흉내낸다."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = False
        self.executions: list[Any] = []

    async def get_executions(self) -> list[Any]:
        if self.fail:
            raise RuntimeError("KIS HTTP 500")
        return self.executions


async def test_buy_cancel_refreshes_unseen_partial_fill_before_flat(sf) -> None:
    """긴급중지: 매수 취소 직전의 미반영 부분체결을 반영해 그 보유까지 청산한다 (flat 오판 방지)."""
    from short_trading_bot.execution.types import Execution

    broker = _ExecBroker()
    svc, _, _ = _svc(sf, broker)
    lot = await svc._spawn("005930", _TMPL)
    await svc._submit(lot, Side.BUY, Decimal(10), Decimal(9000), is_add=False, reason="enter")
    broker.executions = [Execution(
        exec_id="B1:3", broker_order_no="B1", ticker="005930", side=Side.BUY,
        qty=Decimal(3), price=Decimal(9000),
    )]
    broker.fail = True  # 취소 직후 반영 실패
    await svc._flat_all()
    assert lot.qty == 0 and not svc.is_flat()  # 미확인 → 아직 flat 아님 (긴급중지 유지)

    broker.fail = False
    svc._last_price["005930"] = Decimal(9000)
    await svc._flat_all()  # 반영 재시도 → 3주 발견 → 청산 매도
    assert lot.qty == Decimal(3)
    assert broker.submitted[-1].side is Side.SELL and broker.submitted[-1].qty == Decimal(3)
    assert not svc.is_flat()


async def test_orphan_lot_in_occupied_slot_is_liquidated_by_kill_switch(sf) -> None:
    """재오픈됐지만 슬롯이 보유 중인 새 랏에 점유된 고아 랏도 flat 판정·청산 대상이다."""
    svc, broker, _ = _svc(sf)
    old = await svc._spawn("035420", _TMPL)
    await svc._submit(old, Side.BUY, Decimal(10), Decimal(9000), is_add=False, reason="enter")
    old_buy = svc._pending[(old.lot_id, Side.BUY)]
    await svc._on_fill(Fill(old_buy, Decimal(4), Decimal(9000)))
    await svc._submit(old, Side.SELL, Decimal(4), Decimal(9000), is_add=False, reason="hard_stop")
    await svc._on_fill(Fill(svc._pending[(old.lot_id, Side.SELL)], Decimal(4), Decimal(9000)))
    new = await svc._spawn("035420", _TMPL)  # 슬롯에 새 랏 — 보유까지
    await svc._submit(new, Side.BUY, Decimal(2), Decimal(9000), is_add=False, reason="enter")
    await svc._on_fill(Fill(svc._pending[(new.lot_id, Side.BUY)], Decimal(2), Decimal(9000)))

    await svc._on_fill(Fill(old_buy, Decimal(6), Decimal(9000)))  # 옛 매수 잔량 체결 → 고아
    assert old.qty == Decimal(6) and svc.lot("035420") is new
    assert not svc.is_flat()

    svc._last_price["035420"] = Decimal(9000)
    await svc._flat_all()
    sold = {(r.lot_id, r.qty) for r in broker.submitted if r.side is Side.SELL}
    assert (old.lot_id, Decimal(6)) in sold and (new.lot_id, Decimal(2)) in sold


async def test_exit_quantity_recomputed_after_buy_cancel_refresh(sf) -> None:
    """EXIT: 매수 취소 뒤 반영된 미확인 체결분까지 포함한 전량을 매도한다."""
    from short_trading_bot.domain.enums import Resolution
    from short_trading_bot.domain.signal import Intent, IntentKind
    from short_trading_bot.execution.types import Execution
    from short_trading_bot.market.types import Bar

    broker = _ExecBroker()
    svc, _, _ = _svc(sf, broker)
    lot = await svc._spawn("005930", _TMPL)
    await svc._submit(lot, Side.BUY, Decimal(10), Decimal(9000), is_add=False, reason="enter")
    await svc._on_fill(Fill(svc._pending[(lot.lot_id, Side.BUY)], Decimal(4), Decimal(9000)))
    broker.executions = [Execution(  # 브로커 누적 7주 — 3주는 아직 미반영
        exec_id="B1:7", broker_order_no="B1", ticker="005930", side=Side.BUY,
        qty=Decimal(7), price=Decimal(9000),
    )]
    bar = Bar(
        "005930", Resolution.D1, datetime(2026, 9, 25, tzinfo=UTC),
        Decimal(9000), Decimal(9000), Decimal(9000), Decimal(9000), Decimal(1), Decimal(9000),
    )
    intent = Intent(kind=IntentKind.EXIT, side=Side.SELL, reason="hard_stop")
    await svc._handle_intent(intent, lot, bar, svc._risk_snapshot(Decimal(10**8)))
    assert lot.qty == Decimal(7)
    assert broker.submitted[-1].side is Side.SELL and broker.submitted[-1].qty == Decimal(7)


async def test_restart_blocks_flat_and_sells_until_first_fill_refresh(sf) -> None:
    """취소 성공 뒤 반영 전에 죽으면 메모리 장벽이 사라진다 → 재시작 후 첫 폴링 성공 전엔
    flat 도, 매도 재제출도 없다."""
    s1, _, _ = _svc(sf)
    lot = await s1._spawn("005930", _TMPL)
    await s1._submit(lot, Side.BUY, Decimal(10), Decimal(9000), is_add=False, reason="enter")
    await s1._on_fill(Fill(s1._pending[(lot.lot_id, Side.BUY)], Decimal(10), Decimal(9000)))

    broker = _ExecBroker()
    broker.fail = True
    s2, _, _ = _svc(sf, broker)
    await s2.hydrate()
    held = s2.lot("005930")
    assert held is not None and not s2.is_flat()
    n = len(broker.submitted)
    assert not await s2._submit(held, Side.SELL, held.qty, Decimal(9000), is_add=False, reason="kill_switch")
    assert len(broker.submitted) == n

    broker.fail = False
    assert await s2._submit(held, Side.SELL, held.qty, Decimal(9000), is_add=False, reason="kill_switch")
    assert s2._startup_refresh_pending is False


async def test_hydrate_indexes_both_open_rows_sharing_a_slot(sf) -> None:
    """같은 종목·해상도의 열린 행이 둘이면 슬롯엔 하나, 추적(청산)은 둘 다."""
    s1, _, _ = _svc(sf)
    old = await s1._spawn("035420", _TMPL)
    await s1._submit(old, Side.BUY, Decimal(3), Decimal(9000), is_add=False, reason="enter")
    await s1._on_fill(Fill(s1._pending[(old.lot_id, Side.BUY)], Decimal(3), Decimal(9000)))
    new = await s1._spawn("035420", _TMPL)
    await s1._submit(new, Side.BUY, Decimal(2), Decimal(9000), is_add=False, reason="enter")
    await s1._on_fill(Fill(s1._pending[(new.lot_id, Side.BUY)], Decimal(2), Decimal(9000)))

    s2, broker2, _ = _svc(sf)
    await s2.hydrate()
    tracked = {lot.lot_id: lot.qty for lot in s2._tracked_lots()}
    assert tracked == {old.lot_id: Decimal(3), new.lot_id: Decimal(2)}
    s2._last_price["035420"] = Decimal(9000)
    await s2._flat_all()
    assert {(r.lot_id, r.qty) for r in broker2.submitted if r.side is Side.SELL} == {
        (old.lot_id, Decimal(3)), (new.lot_id, Decimal(2)),
    }


async def test_incomplete_overseas_poll_keeps_only_overseas_barriers(sf) -> None:
    """라우팅 어댑터가 해외 조회 실패를 빈 목록으로 강등하면 해외 랏의 반영 장벽은 유지,
    국내 랏의 장벽은 해제 (해외 조회 중단이 국내 청산을 막지 않게)."""
    from short_trading_bot.domain.enums import Market
    from short_trading_bot.execution.broker.routing import RoutingBrokerAdapter

    class _OverseasDown(RestingBroker):
        async def get_executions(self) -> list[Any]:
            raise RuntimeError("overseas 500")

    routing = RoutingBrokerAdapter(RestingBroker(), _OverseasDown())
    await routing.get_executions()
    assert routing.executions_complete is False

    svc = TradingService(routing, sf, RiskManager(RiskLimits()), {})
    kr = await svc._spawn("005930", _TMPL)
    us = await svc._spawn("AAPL", StrategyTemplate(strategy_id="trend_long_v1", market=Market.NASD))
    svc._refresh_before_sell |= {kr.lot_id, us.lot_id}
    svc._unconfirmed_buy_cancels |= {kr.lot_id, us.lot_id}

    assert await svc._refresh_fills(kr) is True
    assert svc._refresh_before_sell == {us.lot_id}
    assert svc._unconfirmed_buy_cancels == {us.lot_id}
    assert await svc._refresh_fills(us) is False
