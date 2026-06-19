from __future__ import annotations

from fastapi import APIRouter, Depends

import short_trading_bot.strategy.algorithms  # noqa: F401  (ensure plugins registered)

from ...strategy.registry import all_strategies
from ..schemas import StrategyInfo
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["strategies"])


@router.get("/strategies", response_model=list[StrategyInfo])
def list_strategies(_user: str = Depends(require_auth)) -> list[StrategyInfo]:
    """Registered algorithms + their pydantic param JSON-schema (UI renders a dynamic form)."""
    return [
        StrategyInfo(
            id=cls.meta.id,
            name=cls.meta.name,
            description=cls.meta.description,
            version=cls.meta.version,
            params_schema=cls.ParamsModel.model_json_schema(),
        )
        for cls in all_strategies().values()
    ]
