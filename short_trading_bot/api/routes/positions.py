from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from ...persistence.db import session_scope
from ...persistence.models import Campaign, Position
from ..schemas import CampaignOut, PositionOut
from ..security import get_state, require_auth

router = APIRouter(prefix="/api", tags=["portfolio"])


@router.get("/positions", response_model=list[PositionOut])
async def list_positions(request: Request, _user: str = Depends(require_auth)) -> list[PositionOut]:
    sf = get_state(request).session_factory
    async with session_scope(sf) as session:
        rows = (await session.execute(select(Position))).scalars().all()
    return [
        PositionOut(
            lot_id=p.lot_id,
            ticker=p.ticker,
            market=p.market,
            currency=p.currency,
            state=p.state,
            strategy_id=p.strategy_id,
            qty_filled=str(p.qty_filled),
            avg_entry_price=str(p.avg_entry_price),
            realized_pnl=str(p.realized_pnl),
            created_at=p.created_at,
        )
        for p in rows
    ]


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(request: Request, _user: str = Depends(require_auth)) -> list[CampaignOut]:
    sf = get_state(request).session_factory
    async with session_scope(sf) as session:
        rows = (await session.execute(select(Campaign))).scalars().all()
    return [
        CampaignOut(
            campaign_id=c.campaign_id,
            name=c.name,
            status=c.status,
            initial_budget=str(c.initial_budget),
            currency=c.currency,
            start_at=c.start_at,
            end_at=c.end_at,
        )
        for c in rows
    ]
