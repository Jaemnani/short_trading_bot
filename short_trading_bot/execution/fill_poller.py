"""FillPoller — ground-truth fill delivery by polling the broker's 체결내역.

KIS REST does not push fills. Rather than parse the encrypted 체결통보 WebSocket (AES + version-
specific field indices — error-prone), we poll the broker's authoritative executed-trade record
(``get_executions``), which IS the source of truth (exact qty/price/fee). Dedup by ``exec_id``,
resolve ``broker_order_no`` -> ``client_order_id`` via the Orders table, and push a Fill to the
same composed handler the engine uses. Safe across disconnects (re-poll recovers anything missed);
pair with the Reconciler (broker 잔고 = source of truth) as a second safety net.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..infra.logging import get_logger
from ..persistence.db import session_scope
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
        """Apply any new executions; returns how many fills were delivered."""
        applied = 0
        for ex in await self._broker.get_executions():
            if ex.exec_id in self._seen:
                continue
            self._seen.add(ex.exec_id)
            client_order_id = await self._resolve(ex.broker_order_no)
            if client_order_id is None:
                self._log.warning("fill_poll.unresolved", broker_order_no=ex.broker_order_no)
                continue
            await self._handler(
                Fill(
                    client_order_id=client_order_id,
                    qty=ex.qty,
                    price=ex.price,
                    fee=ex.fee,
                    tax=ex.tax,
                    currency=ex.currency,
                    ts=ex.ts,
                    source="poll",
                )
            )
            applied += 1
        return applied

    async def _resolve(self, broker_order_no: str) -> str | None:
        async with session_scope(self._sf) as session:
            row = (
                await session.execute(
                    select(Order).where(Order.broker_order_no == broker_order_no)
                )
            ).scalar_one_or_none()
            return row.client_order_id if row is not None else None
