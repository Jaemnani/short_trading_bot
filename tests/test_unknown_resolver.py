from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from short_trading_bot.domain.enums import Side
from short_trading_bot.execution.broker.base import BrokerAdapter
from short_trading_bot.execution.types import (
    AccountBalance,
    OrderAck,
    OrderRecord,
    OrderRequest,
)
from short_trading_bot.execution.unknown_resolver import UnknownOrderResolver
from short_trading_bot.persistence.db import session_scope
from short_trading_bot.persistence.models import Order, Position


class FakeBroker(BrokerAdapter):
    def __init__(self, daily_orders: list[OrderRecord]) -> None:
        self.daily_orders = daily_orders

    @property
    def name(self) -> str:
        return "fake"

    async def submit_order(self, req: OrderRequest) -> OrderAck:
        raise NotImplementedError

    async def cancel_order(self, req: OrderRequest, broker_order_no: str | None) -> OrderAck:
        raise NotImplementedError

    async def get_balance(self) -> AccountBalance:
        return AccountBalance(cash={}, positions=[])

    async def get_open_orders(self) -> list[OrderAck]:
        return []

    async def get_daily_orders(self) -> list[OrderRecord]:
        return self.daily_orders


async def _seed(
    sf,
    *,
    cid: str = "c1",
    lot_id: str = "lot1",
    state: str = "UNKNOWN",
    broker_no: str | None = None,
    age_seconds: float = 0,
) -> None:
    async with session_scope(sf) as s:
        if await s.get(Position, lot_id) is None:
            s.add(
                Position(
                    lot_id=lot_id, ticker="005930", market="KRX", currency="KRW",
                    side="BUY", state="WATCHING", strategy_id="s", params_json={},
                )
            )
        s.add(
            Order(
                order_id=f"o-{cid}", lot_id=lot_id, client_order_id=cid,
                broker_order_no=broker_no, side="BUY", qty=Decimal("10"),
                price=Decimal("70000"), state=state,
                created_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
            )
        )


async def _order_state(sf, cid: str) -> tuple[str, str | None]:
    async with session_scope(sf) as s:
        row = (
            await s.execute(select(Order).where(Order.client_order_id == cid))
        ).scalar_one()
        return row.state, row.broker_order_no


def _record(no: str, qty: str = "10") -> OrderRecord:
    return OrderRecord(broker_order_no=no, ticker="005930", side=Side.BUY, qty=Decimal(qty))


async def test_adopts_unique_match(sf) -> None:
    """타임아웃으로 UNKNOWN이 된 주문이 브로커 주문내역과 1:1 매칭되면 주문번호를 입양한다."""
    await _seed(sf)
    resolver = UnknownOrderResolver(FakeBroker([_record("B1")]), sf)
    assert await resolver.poll_once() == 1
    state, broker_no = await _order_state(sf, "c1")
    assert state == "NEW" and broker_no == "B1"


async def test_ambiguous_match_left_locked(sf) -> None:
    """동일 시그니처(종목·방향·수량) N:N은 절대 추측하지 않는다."""
    await _seed(sf, cid="c1", age_seconds=600)
    await _seed(sf, cid="c2", age_seconds=600)
    resolver = UnknownOrderResolver(FakeBroker([_record("B1"), _record("B2")]), sf)
    assert await resolver.poll_once() == 0
    assert (await _order_state(sf, "c1"))[0] == "UNKNOWN"
    assert (await _order_state(sf, "c2"))[0] == "UNKNOWN"


async def test_linked_broker_order_not_reused(sf) -> None:
    """이미 다른 로컬 주문에 연결된 브로커 주문번호는 후보에서 제외."""
    await _seed(sf, cid="linked", state="NEW", broker_no="B1")
    await _seed(sf, cid="c1", age_seconds=600)
    resolver = UnknownOrderResolver(FakeBroker([_record("B1")]), sf)
    assert await resolver.poll_once() == 1  # 후보 없음 + grace 경과 → REJECTED로 만료
    state, broker_no = await _order_state(sf, "c1")
    assert state == "REJECTED" and broker_no is None


async def test_no_candidate_young_order_stays(sf) -> None:
    """grace 이내의 UNKNOWN은 ack 지연일 수 있으니 건드리지 않는다."""
    await _seed(sf, cid="c1", age_seconds=0)
    resolver = UnknownOrderResolver(FakeBroker([]), sf)
    assert await resolver.poll_once() == 0
    assert (await _order_state(sf, "c1"))[0] == "UNKNOWN"


async def test_no_candidate_old_order_expires(sf) -> None:
    """grace를 넘긴 무후보 UNKNOWN은 REJECTED로 만료해 (lot, side) 락을 푼다."""
    await _seed(sf, cid="c1", age_seconds=600)
    resolver = UnknownOrderResolver(FakeBroker([]), sf)
    assert await resolver.poll_once() == 1
    assert (await _order_state(sf, "c1"))[0] == "REJECTED"
