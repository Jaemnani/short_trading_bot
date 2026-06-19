"""Signal (a candidate to open a lot) and Intent (what a strategy wants to do)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .enums import Market, Side


class IntentKind(StrEnum):
    ENTER = "ENTER"  # open the position (first buy)
    ADD = "ADD"  # 분할매수 / pyramid
    TRIM = "TRIM"  # 분할매도 (partial sell)
    EXIT = "EXIT"  # close the remaining position
    HOLD = "HOLD"  # do nothing


@dataclass(slots=True)
class Intent:
    """A strategy's decision. BUY-side for ENTER/ADD, SELL-side for TRIM/EXIT."""

    kind: IntentKind
    side: Side
    qty: Decimal | None = None  # absolute quantity (ENTER/ADD/EXIT)
    fraction: float | None = None  # fraction of current position (TRIM)
    ord_dvsn: str = "01"  # 01 market, 00 limit
    price: Decimal = Decimal(0)  # limit price (0 => market)
    stop_price: Decimal | None = None  # initial stop set at entry (ENTER)
    reason: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.kind is not IntentKind.HOLD


@dataclass(slots=True)
class Signal:
    """A candidate to create a PositionLot, produced by a scan/manual pick."""

    ticker: str
    market: Market = Market.KRX
    generated_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)
