"""UnknownOrderResolver — recover orders stranded in UNKNOWN after a submit timeout.

A submit timeout leaves the local order UNKNOWN with no broker_order_no: if KIS actually
accepted it, its fills can never be matched (FillPoller resolves by broker_order_no) and
the (lot, side) duplicate-order lock holds forever. This resolver backtracks against the
broker's 일별주문내역 (체결+미체결 전체):

- ADOPT: exactly one broker order matches (ticker, side, qty) and is not already linked
  to any local order → take its 주문번호, transition UNKNOWN → NEW. Fills then flow
  normally on the next poll. Ambiguous signatures (N:N) are left alone — adopting the
  wrong order is worse than staying locked (the Reconciler still catches drift).
- EXPIRE: no unlinked broker order matches and the local order is older than
  ``grace_seconds`` → the order never reached KIS; transition UNKNOWN → REJECTED, which
  releases the duplicate-order lock (service._release_if_terminal).

The grace period covers ack-persistence lag right after a timeout; keep it comfortably
above the broker HTTP timeout.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..domain.enums import OrderState
from ..infra.logging import get_logger
from ..persistence.db import session_scope
from ..persistence.models import AuditLog, Order, Position
from .broker.base import BrokerAdapter
from .order_manager import order_in_session


class UnknownOrderResolver:
    def __init__(
        self,
        broker: BrokerAdapter,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        grace_seconds: float = 180.0,
        logger: Any = None,
    ) -> None:
        self._broker = broker
        self._sf = session_factory
        self._grace = timedelta(seconds=grace_seconds)
        self._log = logger or get_logger("unknown_resolver")

    async def poll_once(self) -> int:
        """Resolve what can be resolved; returns adopted + expired count."""
        async with session_scope(self._sf) as session:
            unknowns = await self._load_unknowns(session)
            if not unknowns:
                return 0
            # '이미 연결된 번호'는 오늘 주문만 — 주문번호는 거래일 단위라 과거 번호까지 넣으면
            # 오늘 실제 접수된 주문이 후보에서 빠져 만료 → 재주문(이중 주문)된다 (#15).
            linked = {
                row.broker_order_no
                for row in (
                    await session.execute(
                        select(Order.broker_order_no, Order.created_at, Position.market)
                        .outerjoin(Position, Order.lot_id == Position.lot_id)
                        .where(Order.broker_order_no.is_not(None))
                    )
                ).all()
                if order_in_session(row.created_at, row.market)
            }
        records = await self._broker.get_daily_orders()

        # Signature → candidate broker orders that no local order owns yet.
        candidates: dict[tuple[str, str, str], list[str]] = {}
        for rec in records:
            if rec.broker_order_no in linked:
                continue
            key = (rec.ticker, rec.side.value, str(rec.qty.normalize()))
            candidates.setdefault(key, []).append(rec.broker_order_no)
        by_sig: dict[tuple[str, str, str], list[tuple[Order, str]]] = {}
        for order, ticker in unknowns:
            key = (ticker, order.side, str(order.qty.normalize()))
            by_sig.setdefault(key, []).append((order, ticker))

        resolved = 0
        now = datetime.now(UTC)
        async with session_scope(self._sf) as session:
            for key, group in by_sig.items():
                found = candidates.get(key, [])
                if len(group) == 1 and len(found) == 1:
                    order, _ticker = group[0]
                    row = await session.get(Order, order.order_id)
                    if row is None or row.state not in _ADOPTABLE_STATES or row.broker_order_no:
                        continue
                    revived = row.state == OrderState.REJECTED.value
                    row.broker_order_no = found[0]
                    row.state = OrderState.NEW.value
                    session.add(_audit(
                        "order.unknown.readopted" if revived else "order.unknown.adopted",
                        row, {"broker_order_no": found[0]},
                    ))
                    self._log.info(
                        "unknown.adopted",
                        client_order_id=row.client_order_id,
                        broker_order_no=found[0],
                    )
                    resolved += 1
                elif not found:
                    for order, _ticker in group:
                        if order.state != OrderState.UNKNOWN.value:
                            continue  # 이미 추정 만료된 주문 — 다시 만료시킬 것 없음 (채택만 대기)
                        created = order.created_at
                        if created.tzinfo is None:  # SQLite drops tzinfo on DateTime columns
                            created = created.replace(tzinfo=UTC)
                        if now - created < self._grace:
                            continue
                        row = await session.get(Order, order.order_id)
                        if row is None or row.state != OrderState.UNKNOWN.value:
                            continue
                        row.state = OrderState.REJECTED.value
                        session.add(_audit("order.unknown.expired", row, {}))
                        self._log.info("unknown.expired", client_order_id=row.client_order_id)
                        resolved += 1
                else:  # ambiguous N:N — never guess; leave locked for the Reconciler
                    self._log.warning(
                        "unknown.ambiguous",
                        signature=key,
                        local=len(group),
                        broker=len(found),
                    )
        return resolved

    async def _load_unknowns(self, session: AsyncSession) -> list[tuple[Order, str]]:
        """UNKNOWN 주문 + 이 resolver 가 '주문내역에 없음'으로 추정 만료시킨 오늘 주문.

        추정 만료(REJECTED)는 주문번호가 없어 체결이 와도 FillPoller 가 매칭하지 못한다.
        늦게라도 브로커 주문내역에 나타나면 채택해야 체결이 반영되고 잠금이 되살아난다."""
        rows = (
            await session.execute(
                select(Order, Position.ticker, Position.market)
                .join(Position, Order.lot_id == Position.lot_id)
                .where(
                    Order.state.in_(_ADOPTABLE_STATES),
                    Order.broker_order_no.is_(None),
                )
            )
        ).all()
        payloads: list[dict[str, Any]] = list(
            (
                await session.execute(
                    select(AuditLog.payload_json).where(AuditLog.event_type == "order.unknown.expired")
                )
            ).scalars()
        )
        presumed = {str((p or {}).get("client_order_id")) for p in payloads}
        return [
            (order, ticker)
            for order, ticker, market in rows
            if order.state == OrderState.UNKNOWN.value
            or (order.client_order_id in presumed and order_in_session(order.created_at, market))
        ]


_ADOPTABLE_STATES = (OrderState.UNKNOWN.value, OrderState.REJECTED.value)


def _audit(event_type: str, order: Order, extra: dict[str, Any]) -> AuditLog:
    return AuditLog(
        event_type=event_type,
        lot_id=order.lot_id,
        payload_json={"client_order_id": order.client_order_id, **extra},
    )
