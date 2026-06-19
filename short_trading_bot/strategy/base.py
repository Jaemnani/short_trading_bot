"""Strategy plugin contract.

Every algorithm implements :class:`Strategy` (single ``evaluate`` that branches on the
position state) and declares a :class:`StrategyMeta` + a pydantic ``ParamsModel`` so the
UI/API can render and validate its parameters. Algorithms are registered with
``@register_strategy`` (see :mod:`registry`) and selected per position.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel

from ..domain.enums import PositionState, Resolution
from ..domain.params import PositionParams
from ..domain.signal import Intent
from ..market.types import IndicatorSnapshot


@dataclass(slots=True)
class StrategyContext:
    """Everything a strategy sees on one evaluation tick."""

    snapshot: IndicatorSnapshot
    state: PositionState
    qty: Decimal
    avg_entry: Decimal
    peak_price: Decimal
    bars_held: int
    params: PositionParams
    equity: Decimal
    initial_stop: Decimal | None = None
    original_qty: Decimal = Decimal(0)  # intended entry size; basis for TP fractions
    tp_rungs_taken: int = 0
    prev: IndicatorSnapshot | None = None
    news_ewma: float | None = None
    now: datetime | None = None

    def ind(self, key: str) -> float | None:
        return self.snapshot.get(key)

    def prev_ind(self, key: str) -> float | None:
        return self.prev.get(key) if self.prev is not None else None


class StrategyMeta(BaseModel):
    id: str
    name: str
    version: str = "1"
    description: str = ""
    supported_resolutions: list[Resolution] | None = None  # None = all


class Strategy(ABC):
    #: Set on each concrete subclass.
    meta: ClassVar[StrategyMeta]
    ParamsModel: ClassVar[type[BaseModel]]

    def __init__(self, params: BaseModel) -> None:
        self.params = params

    @property
    def warmup_bars(self) -> int:
        return 60

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> list[Intent]:
        """Return intents for the current state (ENTER when WATCHING; TRIM/EXIT/HOLD when open)."""
