"""KIS real-time WebSocket feed (국내 체결가 H0STCNT0 -> ticks -> bars).

Implements the Feed interface for live trading. The connection is injectable so message
parsing (the tricky part) is unit-testable offline; the default connector uses ``websockets``.
Heartbeat (PINGPONG) frames are echoed.

H0STCNT0 field indices verified vs the KIS sample repo: time=1, price=2, per-trade
volume CNTG_VOL=12 (NOT 13 — index 13 is ACML_VOL accumulated volume). Subscribe with the
WebSocket approval_key (NOT the REST Bearer token).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..domain.enums import Resolution
from ..infra.logging import get_logger
from .bar_builder import BarBuilder
from .types import Bar, Tick

KST = timezone(timedelta(hours=9))
_log = get_logger("kis_ws")
_TR_TRADE = "H0STCNT0"
_IDX_TIME, _IDX_PRICE, _IDX_VOLUME = 1, 2, 12  # CNTG_VOL (per-trade); 13 = ACML_VOL (accumulated)


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
        bar_builder: BarBuilder | None = None,
        bar_builders: list[BarBuilder] | None = None,
        flush_on_close: bool = True,
    ) -> None:
        """``bar_builder(s)``를 공유하면 재접속(새 피드 인스턴스) 간에 만들던 봉이 보존된다.
        공유 시에는 ``flush_on_close=False``로 두어 부분 봉이 조기 방출되지 않게 할 것.
        ``bar_builders``로 여러 해상도의 빌더를 주면 한 WS 연결(틱)에서 동시에 집계된다
        (KIS는 appkey당 WS 1연결이라 멀티 해상도는 이 방식이 유일하다)."""
        self._approval = approval_key
        self._tickers = tickers
        self._tr_id = tr_id
        self._url = ws_url
        self._connect = connect or self._default_connect
        self._builders = bar_builders or [bar_builder or BarBuilder(resolution)]
        self._flush_on_close = flush_on_close
        self._session_date = session_date
        self._ws: Any = None  # live connection while streaming (dynamic subscribe)

    async def subscribe(self, ticker: str) -> bool:
        """스트리밍 중 동적 구독 (장중 스캐너 합류). 티커 리스트를 공유하면 재접속
        시에도 유지된다. 이미 구독 중이면 False."""
        if ticker in self._tickers:
            return False
        self._tickers.append(ticker)
        if self._ws is not None:
            await self._ws.send(self._subscribe_frame(ticker))
        return True

    async def stream(self) -> AsyncIterator[Bar]:
        try:
            async with self._connect() as ws:
                self._ws = ws
                for ticker in list(self._tickers):
                    await ws.send(self._subscribe_frame(ticker))
                async for raw in ws:
                    if raw and raw[0] == "{":  # JSON control frame
                        if "PINGPONG" in raw:
                            await ws.send(raw)  # echo heartbeat
                        continue
                    # 프레임마다 날짜를 다시 잡는다 — 연결이 자정을 넘기면 고정 날짜로는 다음 날
                    # 틱이 전날 시각으로 찍혀 역순이 된다.
                    base_date = self._session_date or datetime.now(KST).date()
                    for tick in self.parse_ticks(raw, base_date, tr_id=self._tr_id):
                        for builder in self._builders:
                            try:
                                bars = builder.on_tick(tick)
                            except ValueError:
                                # 역순 틱 1개로 스트림 전체를 끊지 않는다 — 끊기면 재접속 동안
                                # 모든 종목의 손절 관리가 멈춘다 (#18). 그 틱만 버린다.
                                _log.warning("ws.out_of_order_tick", ticker=tick.ticker, ts=str(tick.ts))
                                continue
                            for bar in bars:
                                yield bar
                if self._flush_on_close:  # 공유 빌더는 flush 금지 (부분 봉 조기 방출 방지)
                    for builder in self._builders:
                        for bar in builder.flush():
                            yield bar
        finally:
            self._ws = None

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
            tick = _parse_record(rec, base_date)
            if tick is None:
                # 깨진 레코드(시각 형식 오류, NaN·음수·0 가격 등)는 버린다. 예외로 두면
                # 스트림이 끊기고, 그대로 받으면 가짜 가격이 전략·주문가로 흘러간다 (#18).
                _log.warning("ws.bad_tick_record", tr_id=tr_id, record=rec[:3])
                continue
            ticks.append(tick)
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


def _parse_record(rec: list[str], base_date: Any) -> Tick | None:
    """KIS 체결 레코드 1건 → Tick. 검증 실패면 None."""
    hhmmss = rec[_IDX_TIME]
    if len(hhmmss) < 6 or not hhmmss[:6].isdigit():
        return None
    try:
        ts = datetime(
            base_date.year, base_date.month, base_date.day,
            int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6]), tzinfo=KST,
        )
        price = Decimal(rec[_IDX_PRICE])
        volume = Decimal(rec[_IDX_VOLUME])
    except (ValueError, ArithmeticError):
        return None
    if not price.is_finite() or price <= 0 or not volume.is_finite() or volume < 0:
        return None
    return Tick(rec[0], price, volume, ts)
