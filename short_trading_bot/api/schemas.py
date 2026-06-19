"""API request/response models. Money fields are strings to preserve Decimal precision."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class StrategyInfo(BaseModel):
    id: str
    name: str
    description: str
    version: str
    params_schema: dict[str, Any]


class ControlIn(BaseModel):
    action: Literal["pause", "resume", "stop"]
    scope: str | None = None


class ControlOut(BaseModel):
    state: str
    flat_all_requested: bool
    scope: str | None


class PositionOut(BaseModel):
    lot_id: str
    ticker: str
    market: str
    currency: str
    state: str
    strategy_id: str
    qty_filled: str
    avg_entry_price: str
    realized_pnl: str
    created_at: datetime | None = None


class CampaignOut(BaseModel):
    campaign_id: str
    name: str
    status: str
    initial_budget: str
    currency: str
    start_at: datetime
    end_at: datetime
