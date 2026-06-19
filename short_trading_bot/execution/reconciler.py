"""Reconcile local position state against the broker (source of truth).

Run on startup, periodically, and after any disconnect — before resuming trading.
P1 detects + reports drift (and writes an audit entry); automated repair is layered on later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..domain.enums import PositionState
from ..infra.logging import get_logger
from ..persistence.db import session_scope
from ..persistence.models import AuditLog, Position
from .broker.base import BrokerAdapter

_OPEN_STATES = (
    PositionState.HOLDING.value,
    PositionState.SCALING.value,
    PositionState.EXITING.value,
)


@dataclass(slots=True)
class Drift:
    ticker: str
    local_qty: Decimal
    broker_qty: Decimal

    @property
    def delta(self) -> Decimal:
        return self.broker_qty - self.local_qty


@dataclass(slots=True)
class ReconcileReport:
    drifts: list[Drift] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        return all(d.delta == 0 for d in self.drifts)

    @property
    def mismatches(self) -> list[Drift]:
        return [d for d in self.drifts if d.delta != 0]


class Reconciler:
    def __init__(
        self,
        broker: BrokerAdapter,
        session_factory: async_sessionmaker[AsyncSession],
        logger: Any = None,
    ) -> None:
        self._broker = broker
        self._sf = session_factory
        self._log = logger or get_logger("reconciler")

    async def reconcile(self) -> ReconcileReport:
        balance = await self._broker.get_balance()
        broker_qty: dict[str, Decimal] = {}
        for p in balance.positions:
            broker_qty[p.ticker] = broker_qty.get(p.ticker, Decimal(0)) + p.qty

        async with session_scope(self._sf) as s:
            local_qty = await self._local_qty(s)
            report = ReconcileReport(
                drifts=[
                    Drift(
                        ticker=ticker,
                        local_qty=local_qty.get(ticker, Decimal(0)),
                        broker_qty=broker_qty.get(ticker, Decimal(0)),
                    )
                    for ticker in sorted(set(local_qty) | set(broker_qty))
                ]
            )
            if not report.in_sync:
                self._log.warning(
                    "reconcile.drift",
                    mismatches=[(d.ticker, str(d.delta)) for d in report.mismatches],
                )
                s.add(
                    AuditLog(
                        event_type="reconcile.drift",
                        payload_json={d.ticker: str(d.delta) for d in report.mismatches},
                    )
                )
            else:
                self._log.info("reconcile.in_sync", tickers=len(report.drifts))
        return report

    @staticmethod
    async def _local_qty(s: AsyncSession) -> dict[str, Decimal]:
        rows = (
            await s.execute(
                select(Position.ticker, Position.qty_filled).where(
                    Position.state.in_(_OPEN_STATES)
                )
            )
        ).all()
        out: dict[str, Decimal] = {}
        for ticker, qty in rows:
            out[ticker] = out.get(ticker, Decimal(0)) + qty
        return out
