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

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..infra.logging import get_logger
from ..persistence.db import session_scope
from ..persistence.models import Fill as FillRow
from ..persistence.models import Order
from .broker.base import BrokerAdapter
from .order_manager import created_today_kst
from .types import Fill, FillHandler


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


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
        # 델타 회계(ex.qty - DB 누적)는 읽기→쓰기 사이에 다른 폴링이 끼면 같은 체결을 두 번
        # 반영한다. 동시 호출(백그라운드 루프·재접속 복구·주문 교체)을 직렬화한다.
        self._lock = asyncio.Lock()
        self._log = logger or get_logger("fill_poller")

    async def poll_once(self) -> int:
        """Apply any new execution deltas; returns how many fills were delivered."""
        async with self._lock:
            return await self._poll_locked()

    async def _poll_locked(self) -> int:
        applied = 0
        for ex in await self._broker.get_executions():
            if ex.exec_id in self._seen:
                continue
            resolved = await self._resolve(ex.broker_order_no)
            if resolved is None:
                # Ack may not be persisted yet — retry on the next poll (don't mark seen).
                self._log.warning("fill_poll.unresolved", broker_order_no=ex.broker_order_no)
                continue
            client_order_id, already_qty, already_notional, already_fee, already_tax = resolved
            delta = ex.qty - already_qty
            if delta <= 0:  # nothing new (restart replay / duplicate row)
                self._seen.add(ex.exec_id)
                continue
            delta_notional = ex.qty * ex.price - already_notional
            if delta_notional <= 0:
                # ex.price is a broker-rounded cumulative average while already_notional
                # sums exact DB rows — never let rounding produce a zero/negative price.
                self._log.warning(
                    "fill_poll.notional_regression",
                    broker_order_no=ex.broker_order_no,
                    delta=str(delta),
                )
                delta_price = ex.price
            else:
                delta_price = delta_notional / delta
            await self._handler(
                Fill(
                    client_order_id=client_order_id,
                    qty=delta,
                    price=delta_price,
                    fee=max(Decimal(0), ex.fee - already_fee),
                    tax=max(Decimal(0), ex.tax - already_tax),
                    currency=ex.currency,
                    ts=ex.ts,
                    source="poll",
                )
            )
            self._seen.add(ex.exec_id)
            applied += 1
        return applied

    async def _resolve(
        self, broker_order_no: str
    ) -> tuple[str, Decimal, Decimal, Decimal, Decimal] | None:
        """Map an order to already applied cumulative execution totals."""
        async with session_scope(self._sf) as session:
            # KIS 주문번호는 거래일 단위 — 과거 날짜 주문과 번호가 겹칠 수 있다. 체결내역은
            # 오늘자만 조회하므로 오늘 생성된 주문으로 한정한다 (전 기간 scalar_one_or_none 은
            # 번호가 겹치는 날 MultipleResultsFound 로 폴링 전체를 멈추거나 과거 랏에 붙인다 #15).
            candidates = [
                o
                for o in (
                    await session.execute(
                        select(Order).where(Order.broker_order_no == broker_order_no)
                    )
                ).scalars().all()
                if created_today_kst(o.created_at)
            ]
            if not candidates:
                return None
            if len(candidates) > 1:
                self._log.warning(
                    "fill_poll.duplicate_order_no", broker_order_no=broker_order_no, n=len(candidates)
                )
            order = max(candidates, key=lambda o: _aware(o.created_at))
            rows = (
                await session.execute(
                    select(FillRow.qty, FillRow.price, FillRow.fee, FillRow.tax).where(
                        FillRow.order_id == order.order_id
                    )
                )
            ).all()
            qty = sum((row.qty for row in rows), Decimal(0))
            notional = sum((row.qty * row.price for row in rows), Decimal(0))
            fee = sum((row.fee for row in rows), Decimal(0))
            tax = sum((row.tax for row in rows), Decimal(0))
            return order.client_order_id, qty, notional, fee, tax
