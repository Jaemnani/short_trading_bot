"""Shared API state: control switch, DB session factory, and auth config.

Held on ``app.state.api`` and read by routes. The same ControlSwitch instance is shared
with the running TradingService, so a dashboard pause/kill-switch reaches the live engine.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..risk.control import ControlSwitch


@dataclass
class ApiState:
    control: ControlSwitch
    session_factory: async_sessionmaker[AsyncSession]
    jwt_secret: str
    username: str = "admin"
    password: str = "admin"
