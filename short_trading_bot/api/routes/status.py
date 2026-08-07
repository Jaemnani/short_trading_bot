"""실시간 현황 — 엔진 상태 파일(data/engine_status.json) + 오늘 체결(DB) 통합.

엔진은 별도 프로세스라 API가 직접 자산/포지션을 계산할 수 없다. 엔진이 5초마다
쓰는 스냅샷 파일을 읽고, 파일 나이(age)로 엔진 생사를 판정한다 (15초 넘으면 끊김).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from ...persistence.db import session_scope
from ...persistence.models import Fill, Order, Position
from ..security import get_state, require_auth

router = APIRouter(prefix="/api", tags=["status"])

_ALIVE_SECONDS = 15  # 기록 주기 5초의 3배
_KST = timezone(timedelta(hours=9))


def _engine_snapshot(path: Path) -> tuple[dict[str, Any] | None, bool]:
    """(스냅샷, 엔진 생존 여부). 파일 없음/손상 = (None, False)."""
    try:
        snap: dict[str, Any] = json.loads(path.read_text())
        ts = datetime.fromisoformat(str(snap.get("ts")))
        alive = datetime.now(UTC) - ts <= timedelta(seconds=_ALIVE_SECONDS)
        return snap, alive
    except (OSError, ValueError, TypeError):
        return None, False


@router.get("/status")
async def get_status(request: Request, _user: str = Depends(require_auth)) -> dict[str, Any]:
    snap, alive = _engine_snapshot(get_state(request).status_file)
    today_kst = datetime.now(_KST).date()
    day_start = datetime(today_kst.year, today_kst.month, today_kst.day, tzinfo=_KST).astimezone(UTC)

    sf = get_state(request).session_factory
    async with session_scope(sf) as session:
        rows = (
            await session.execute(
                select(Fill, Order.side, Position.ticker)
                .join(Order, Fill.order_id == Order.order_id)
                .join(Position, Fill.lot_id == Position.lot_id)
                .where(Fill.filled_at >= day_start.replace(tzinfo=None))
                .order_by(Fill.filled_at.desc())
                .limit(50)
            )
        ).all()
    fills = [
        {
            "time": f"{fill.filled_at:%H:%M:%S}",
            "ticker": ticker,
            "side": side,
            "qty": str(fill.qty),
            "price": str(fill.price),
            "fee": str(fill.fee),
            "tax": str(fill.tax),
        }
        for fill, side, ticker in rows
    ]
    return {"engine_alive": alive, "engine": snap, "today_fills": fills}
