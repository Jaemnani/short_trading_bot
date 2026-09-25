"""Shared API state: control switch, DB session factory, and auth config.

Held on ``app.state.api`` and read by routes. The same ControlSwitch instance is shared
with the running TradingService, so a dashboard pause/kill-switch reaches the live engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..risk.control import ControlSwitch
from ..risk.control_file import DEFAULT_PATH as CONTROL_FILE_DEFAULT
from ..risk.control_file import KILL_SWITCH_PATH
from .throttle import LoginThrottle


@dataclass
class ApiState:
    control: ControlSwitch
    session_factory: async_sessionmaker[AsyncSession]
    jwt_secret: str
    username: str = "admin"
    password: str = "admin"
    # 프로세스 간 브리지 파일 — 테스트는 tmp 경로로 주입 (실제 엔진 파일 오염 방지)
    control_file: Path = field(default_factory=lambda: CONTROL_FILE_DEFAULT)
    status_file: Path = field(default_factory=lambda: Path("data/engine_status.json"))
    kill_switch_file: Path = field(default_factory=lambda: KILL_SWITCH_PATH)
    # 명시 허용 오리진만 CORS 허용 (빈 목록 = CORS 미들웨어 없음 = 같은 오리진만).
    cors_origins: list[str] = field(default_factory=list)
    login_throttle: LoginThrottle = field(default_factory=LoginThrottle)
