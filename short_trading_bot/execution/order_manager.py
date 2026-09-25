"""Idempotent order management + fill application.

Idempotency: the ``client_order_id`` row is persisted BEFORE the broker call, and a
second ``submit`` with the same id never re-sends — it returns the existing order's
ack. Fills update the order state machine and the owning PositionLot, and every step
is written to the append-only ``audit_log``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..domain.enums import OrderState, PositionState, Side
from ..infra.logging import get_logger
from ..persistence.db import session_scope
from ..persistence.models import AuditLog, Order, Position
from ..persistence.models import Fill as FillRow
from .broker.base import BrokerAdapter
from .types import Fill, OrderAck, OrderRequest

# 이미 종결된 주문 = 취소가 막을 것이 없는 상태 (True 반환).
_CANCEL_NOOP_STATES = frozenset({
    OrderState.FILLED.value, OrderState.CANCELLED.value,
    OrderState.REJECTED.value, OrderState.EXPIRED.value,
})

# 체결 없이 끝난 것으로 확정된 상태 — 늦게 온 체결이 이 상태를 되돌리면 안 된다.
_TERMINAL_NO_FILL_STATES = frozenset({
    OrderState.CANCELLED.value, OrderState.REJECTED.value, OrderState.EXPIRED.value,
})


class OrderManager:
    def __init__(
        self,
        broker: BrokerAdapter,
        session_factory: async_sessionmaker[AsyncSession],
        logger: Any = None,
    ) -> None:
        self._broker = broker
        self._sf = session_factory
        self._log = logger or get_logger("order_manager")
        broker.fill_handler = self.handle_fill  # route fills back here

    async def submit(self, req: OrderRequest) -> OrderAck:
        # 1) Idempotency check + persist PENDING_NEW BEFORE any network call.
        async with session_scope(self._sf) as s:
            existing = await self._find_order(s, req.client_order_id)
            if existing is not None:
                self._log.info(
                    "order.idempotent.skip",
                    client_order_id=req.client_order_id,
                    state=existing.state,
                )
                accepted = existing.state in {
                    OrderState.NEW.value,
                    OrderState.PARTIALLY_FILLED.value,
                    OrderState.FILLED.value,
                }
                return OrderAck(
                    client_order_id=req.client_order_id,
                    accepted=accepted,
                    broker_order_no=existing.broker_order_no,
                    tr_id=existing.tr_id,
                    reject_reason=None if accepted else f"existing_order_{existing.state.lower()}",
                )
            s.add(
                Order(
                    order_id=str(uuid4()),
                    lot_id=req.lot_id,
                    client_order_id=req.client_order_id,
                    side=req.side.value,
                    qty=req.qty,
                    price=req.price,
                    ord_dvsn=req.ord_dvsn,
                    state=OrderState.PENDING_NEW.value,
                )
            )
            s.add(self._audit("order.pending", req.lot_id, {"client_order_id": req.client_order_id}))

        # 2) Network call (outside the persist transaction).
        try:
            ack = await self._broker.submit_order(req)
        except Exception:
            # A timeout does not mean the broker rejected the order. Preserve the
            # ambiguity so callers cannot mistake a stranded PENDING_NEW row for success.
            async with session_scope(self._sf) as s:
                order = await self._find_order(s, req.client_order_id)
                if order is not None:
                    order.state = OrderState.UNKNOWN.value
                    s.add(self._audit("order.unknown", req.lot_id, {"client_order_id": req.client_order_id}))
            raise

        # 3) Record outcome. Guard against overwriting a fill that already arrived.
        async with session_scope(self._sf) as s:
            order = await self._find_order(s, req.client_order_id)
            assert order is not None
            if ack.accepted:
                order.broker_order_no = ack.broker_order_no
                order.tr_id = ack.tr_id
                if order.state == OrderState.PENDING_NEW.value:
                    order.state = OrderState.NEW.value
                evt = "order.accepted"
            else:
                order.state = OrderState.REJECTED.value
                evt = "order.rejected"
            s.add(
                self._audit(
                    evt,
                    req.lot_id,
                    {"client_order_id": req.client_order_id, "reason": ack.reject_reason},
                )
            )
        return ack

    async def cancel(self, req: OrderRequest) -> bool:
        """미체결 주문 취소 — 손절이 기존 익절 주문에 막히지 않게 하는 통로.

        True = 취소 완료(또는 이미 종결이라 막을 게 없음). UNKNOWN 주문은 건드리지
        않는다(False) — 브로커 상태를 모르는 채 취소하면 이중 매도 위험이 있고,
        UnknownOrderResolver가 먼저 정체를 밝혀야 한다."""
        async with session_scope(self._sf) as s:
            order = await self._find_order(s, req.client_order_id)
            if order is None:
                return True
            if order.state in _CANCEL_NOOP_STATES:
                return True
            if order.state == OrderState.UNKNOWN.value:
                return False
            broker_no = order.broker_order_no
        ack = await self._broker.cancel_order(req, broker_no)
        if ack.accepted:
            async with session_scope(self._sf) as s:
                order = await self._find_order(s, req.client_order_id)
                if order is not None and order.state != OrderState.FILLED.value:
                    order.state = OrderState.CANCELLED.value
                    s.add(self._audit(
                        "order.cancelled", req.lot_id, {"client_order_id": req.client_order_id}
                    ))
        return ack.accepted

    async def handle_fill(self, fill: Fill) -> None:
        async with session_scope(self._sf) as s:
            order = await self._find_order(s, fill.client_order_id)
            if order is None:
                self._log.warning("fill.unknown_order", client_order_id=fill.client_order_id)
                return

            s.add(
                FillRow(
                    fill_id=str(uuid4()),
                    order_id=order.order_id,
                    lot_id=order.lot_id,
                    qty=fill.qty,
                    price=fill.price,
                    fee=fill.fee,
                    tax=fill.tax,
                    currency=fill.currency.value,
                    filled_at=fill.ts,
                    source=fill.source,
                )
            )

            total_filled = await self._total_filled(s, order.order_id) + fill.qty
            if order.state in _TERMINAL_NO_FILL_STATES:
                # 취소/만료/거부 확정 뒤 도착한 체결(취소 직전 체결분 등). 체결 자체는 사실이므로
                # 체결 행과 포지션 투영에는 반영하되, 주문 상태는 되살리지 않는다 — 되살리면
                # 이미 끝난 주문이 '열린 주문'으로 보여 (lot, side) 잠금이 다시 걸린다.
                self._log.warning(
                    "fill.after_terminal", client_order_id=fill.client_order_id, state=order.state
                )
            else:
                order.state = (
                    OrderState.FILLED.value
                    if total_filled >= order.qty
                    else OrderState.PARTIALLY_FILLED.value
                )

            await self._apply_to_position(s, order, fill)
            s.add(
                self._audit(
                    "fill.applied",
                    order.lot_id,
                    {
                        "client_order_id": fill.client_order_id,
                        "qty": str(fill.qty),
                        "price": str(fill.price),
                        "order_state": order.state,
                    },
                )
            )

    # -- internals -------------------------------------------------------

    @staticmethod
    async def _find_order(s: AsyncSession, client_order_id: str) -> Order | None:
        return (
            await s.execute(select(Order).where(Order.client_order_id == client_order_id))
        ).scalar_one_or_none()

    @staticmethod
    async def _total_filled(s: AsyncSession, order_id: str) -> Decimal:
        rows = (
            await s.execute(select(FillRow.qty).where(FillRow.order_id == order_id))
        ).scalars().all()
        return sum(rows, Decimal(0))

    async def _apply_to_position(self, s: AsyncSession, order: Order, fill: Fill) -> None:
        pos = (
            await s.execute(select(Position).where(Position.lot_id == order.lot_id))
        ).scalar_one_or_none()
        if pos is None:
            self._log.warning("fill.no_position", lot_id=order.lot_id)
            return

        if order.side == Side.BUY:
            new_qty = pos.qty_filled + fill.qty
            if new_qty > 0:
                pos.avg_entry_price = (
                    pos.avg_entry_price * pos.qty_filled + fill.price * fill.qty
                ) / new_qty
            pos.qty_filled = new_qty
            # Only the WATCHING->HOLDING edge belongs here; richer states (SCALING,
            # EXITING) are owned by the domain lot — stomping them to HOLDING would
            # corrupt what hydrate() restores after a restart. CLOSED + 매수 체결 = 청산 뒤
            # 도착한 매수 잔량: 다시 열어야 hydrate() 가 복원하고 손절이 관리한다.
            if pos.state in (PositionState.WATCHING.value, PositionState.CLOSED.value):
                if pos.state == PositionState.CLOSED.value:
                    self._log.warning("fill.reopen_closed_position", lot_id=order.lot_id)
                    pos.closed_at = None
                pos.state = PositionState.HOLDING.value
        else:  # SELL closes part/all of the long -> realize P&L on the held portion only
            realized_qty = min(fill.qty, pos.qty_filled) if pos.qty_filled > 0 else Decimal(0)
            pos.realized_pnl += (fill.price - pos.avg_entry_price) * realized_qty - fill.fee - fill.tax
            pos.qty_filled = max(Decimal(0), pos.qty_filled - fill.qty)
            if pos.qty_filled == 0:
                pos.state = PositionState.CLOSED.value
                pos.closed_at = datetime.now(UTC)
            if fill.qty > realized_qty:  # over-sell => drift/double-fill; never fabricate PnL
                self._log.warning(
                    "fill.oversell",
                    lot_id=order.lot_id,
                    fill_qty=str(fill.qty),
                    held=str(realized_qty),
                )

    @staticmethod
    def _audit(event_type: str, lot_id: str | None, payload: dict[str, Any]) -> AuditLog:
        return AuditLog(event_type=event_type, lot_id=lot_id, payload_json=payload)
