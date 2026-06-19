"""Kill switch action: build flat-all liquidation intents for open lots.

State (engaged/paused) lives in :class:`~short_trading_bot.risk.control.ControlSwitch`;
this module produces the EXIT intents that close everything. Campaign-scoping is done by
the caller passing only the relevant lots.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..domain.enums import Side
from ..domain.position import PositionLot
from ..domain.signal import Intent, IntentKind


def build_flat_all_intents(lots: Iterable[PositionLot]) -> list[tuple[PositionLot, Intent]]:
    """One market EXIT intent per OPEN lot. Forced liquidation bypasses entry risk checks."""
    return [
        (lot, Intent(kind=IntentKind.EXIT, side=Side.SELL, ord_dvsn="01", reason="kill_switch"))
        for lot in lots
        if lot.is_open
    ]
