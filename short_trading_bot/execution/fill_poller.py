"""FillPoller — ground-truth fill delivery by polling the broker's 체결내역.

KIS REST does not push fills. Rather than parse the encrypted 체결통보 WebSocket (AES + version-
specific field indices — error-prone), we poll the broker's authoritative executed-trade record
(``get_executions``), which IS the source of truth (exact qty/price/fee).

Accounting is DELTA-based against the DB: 체결내역 rows carry the CUMULATIVE executed quantity
per order, so we apply ``ex.qty - already_filled(order)`` and skip non-positive deltas. This
makes polling idempotent across partial fills, process restarts, and duplicate rows — the DB
fills table is the dedup source of truth (the in-memory ``_seen`` set is only a fast-path).
Unresolved broker order numbers are retried on the next poll (the ack may not be persisted yet).
Pair with the Reconciler (broker 잔고 = source of truth) as a second safety net.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..infra.logging import get_logger
from ..persistence.db import session_scope
from ..persistence.models import Fill as FillRow
from ..persistence.models import Order
from .broker.base import BrokerAdapter
from .types import Fill, FillHandler


class FillPoller:
    def __init__(
        self,
        broker: BrokerAdapter,
        session_factory: async_sessionmaker[AsyncSession],
        fill_handler: FillHandler,
        *,
        logger: Any = None,
    ) -> None:
        self._broker = broker
        self._sf = session_factory
        self._handler = fill_handler
        self._seen: set[str] = set()
        self._log = logger or get_logger("fill_poller")

    async def poll_once(self) -> int:
        """Apply any new execution deltas; returns how many fills were delivered."""
        applied = 0
        for ex in await self._broker.get_executions():
            if ex.exec_id in self._seen:
                continue
            resolved = await self._resolve(ex.broker_order_no)
            if resolved is None:
                # Ack may not be persisted yet — retry on the next poll (don't mark seen).
                self._log.warning("fill_poll.unresolved", broker_order_no=ex.broker_order_no)
                continue
            client_order_id, already = resolved
            delta = ex.qty - already
            if delta <= 0:  # nothing new (restart replay / duplicate row)
                self._seen.add(ex.exec_id)
                continue
            await self._handler(
                Fill(
                    client_order_id=client_order_id,
                    qty=delta,
                    price=ex.price,
                    fee=ex.fee,
                    tax=ex.tax,
                    currency=ex.currency,
                    ts=ex.ts,
                    source="poll",
                )
            )
            self._seen.add(ex.exec_id)
            applied += 1
        return applied

    async def _resolve(self, broker_order_no: str) -> tuple[str, Decimal] | None:
        """Map broker_order_no -> (client_order_id, already-filled qty in DB)."""
        async with session_scope(self._sf) as session:
            order = (
                await session.execute(
                    select(Order).where(Order.broker_order_no == broker_order_no)
                )
            ).scalar_one_or_none()
            if order is None:
                return None
            rows = (
                await session.execute(select(FillRow.qty).where(FillRow.order_id == order.order_id))
            ).scalars().all()
            return order.client_order_id, sum(rows, Decimal(0))
