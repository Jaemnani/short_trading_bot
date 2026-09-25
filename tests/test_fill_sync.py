"""체결 → 메모리 랏 동기화 회귀 테스트 (#3 #4 #5).

실계좌에서는 지정가가 걸려 있다가 나중에(폴링으로) 체결된다. 페이퍼 브로커는 즉시
체결이라 이 경로가 안 드러나므로, 주문을 접수만 하고 체결은 테스트가 직접 넣는 브로커를 쓴다.
"""

from __future__ import annotations

from decimal import Decimal

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
