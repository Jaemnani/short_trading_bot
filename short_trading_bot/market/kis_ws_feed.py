"""KIS real-time WebSocket feed (국내 체결가 H0STCNT0 -> ticks -> bars).

Implements the Feed interface for live trading. The connection is injectable so message
parsing (the tricky part) is unit-testable offline; the default connector uses ``websockets``.
Heartbeat (PINGPONG) frames are echoed.

⚠️ The H0STCNT0 field indices (price=2, time=1, volume=13) follow common KIS docs but must be
verified against the portal/sample repo before live use; subscribe with the WebSocket
approval_key (NOT the REST Bearer token).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..domain.enums import Resolution
from .bar_builder import BarBuilder
from .types import Bar, Tick

KST = timezone(timedelta(hours=9))
_TR_TRADE = "H0STCNT0"
_IDX_TIME, _IDX_PRICE, _IDX_VOLUME = 1, 2, 13


class KisWebSocketFeed:
    def __init__(
        self,
        approval_key: str,
        tickers: list[str],
        resolution: Resolution,
        *,
        ws_url: str = "ws://ops.koreainvestment.com:21000",
        tr_id: str = _TR_TRADE,
        connect: Callable[[], Any] | None = None,
        session_date: Any = None,
    ) -> None:
        self._approval = approval_key
        self._tickers = tickers
        self._tr_id = tr_id
        self._url = ws_url
        self._connect = connect or self._default_connect
        self._bar = BarBuilder(resolution)
        self._session_date = session_date

    async def stream(self) -> AsyncIterator[Bar]:
        base_date = self._session_date or datetime.now(KST).date()
        async with self._connect() as ws:
            for ticker in self._tickers:
                await ws.send(self._subscribe_frame(ticker))
            async for raw in ws:
                if raw and raw[0] == "{":  # JSON control frame
                    if "PINGPONG" in raw:
                        await ws.send(raw)  # echo heartbeat
                    continue
                for tick in self.parse_ticks(raw, base_date, tr_id=self._tr_id):
                    for bar in self._bar.on_tick(tick):
                        yield bar
            for bar in self._bar.flush():  # flush open bars on disconnect
                yield bar

    @staticmethod
    def parse_ticks(raw: str, base_date: Any, *, tr_id: str = _TR_TRADE) -> list[Tick]:
        """Parse a KIS realtime data frame ('0|H0STCNT0|<count>|<f^f^...>') into ticks."""
        if not raw or raw[0] == "{":
            return []
        parts = raw.split("|")
        if len(parts) < 4 or parts[1] != tr_id:
            return []
        count = int(parts[2]) if parts[2].isdigit() and int(parts[2]) > 0 else 1
        fields = parts[3].split("^")
        width = len(fields) // count
        ticks: list[Tick] = []
        for i in range(count):
            rec = fields[i * width : (i + 1) * width]
            if len(rec) <= _IDX_VOLUME:
                continue
            hhmmss = rec[_IDX_TIME]
            ts = datetime(
                base_date.year, base_date.month, base_date.day,
                int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6]), tzinfo=KST,
            )
            ticks.append(Tick(rec[0], Decimal(rec[_IDX_PRICE]), Decimal(rec[_IDX_VOLUME]), ts))
        return ticks

    def _subscribe_frame(self, ticker: str) -> str:
        return json.dumps(
            {
                "header": {
                    "approval_key": self._approval,
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": self._tr_id, "tr_key": ticker}},
            }
        )

    def _default_connect(self) -> Any:
        import websockets

        return websockets.connect(self._url)
