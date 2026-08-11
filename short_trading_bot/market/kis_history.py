"""KIS 분봉 히스토리 로더 — 주식일별분봉조회 (FHKST03010230).

Fetches past-day 1-minute candles for a ticker (verified live 2026-07: 120 rows/call,
DESCENDING by time, fields stck_bsop_date/stck_cntg_hour/stck_prpr(close)/stck_oprc/
stck_hgpr/stck_lwpr/cntg_vol). Pages backwards via the FID_INPUT_HOUR_1 cursor until
09:00. Results are cached to disk (JSON per ticker/day) so repeat backtests don't
re-hit the API. Transport is injectable for offline tests.

⚠️ KIS access-token issuance is limited (~1/min): share ONE KisAuth across calls.
Rate-limit REST calls (~3-4/s on 모의) via ``rate_delay``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from ..domain.enums import Resolution
from ..infra.config import KisEnvCreds
from ..infra.http import shared_client
from ..infra.kis_auth import KisAuth
from ..infra.logging import get_logger
from ..infra.rate_limit import shared_limiter
from .types import Bar

Transport = Callable[[str, str, dict[str, str], dict[str, Any]], Awaitable[dict[str, Any]]]

KST = timezone(timedelta(hours=9))
_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
_TR_ID = "FHKST03010230"
_SESSION_START = "090000"


class KisMinuteHistory:
    def __init__(
        self,
        auth: KisAuth,
        creds: KisEnvCreds,
        base_url: str,
        *,
        transport: Transport | None = None,
        cache_dir: str | Path = "data/minutes",
        rate_delay: float = 0.3,
        timeout: float = 10.0,
    ) -> None:
        self._auth = auth
        self._creds = creds
        self._base = base_url.rstrip("/")
        self._transport = transport or self._default_transport
        self._cache = Path(cache_dir)
        self._delay = rate_delay
        self._timeout = timeout
        self._log = get_logger("kis_history")

    async def fetch_day(self, ticker: str, day: date, *, cache: bool = True) -> list[Bar]:
        """해당 일자의 1분봉 전체 (오름차순). 캐시 우선; 미거래일은 빈 리스트.

        ``cache=False``는 진행 중인 '오늘'을 조회할 때 필수 — 부분 하루를 캐시에
        쓰면 이후 조회가 영원히 그 시점까지만 보게 된다."""
        cache_file = self._cache / ticker / f"{day:%Y%m%d}.json"
        if cache and cache_file.exists():
            return self._from_cache(ticker, day, cache_file)

        day_str = f"{day:%Y%m%d}"
        rows_by_time: dict[str, dict[str, Any]] = {}
        cursor = "153000"
        ok = False
        while True:
            data = await self._get(ticker, day_str, cursor)
            if str(data.get("rt_cd")) != "0":
                self._log.warning("minutes.fetch_failed", ticker=ticker, day=day_str,
                                  msg=str(data.get("msg1", "")).strip())
                break
            ok = True
            fresh = [
                r for r in (data.get("output2") or [])
                if r.get("stck_bsop_date") == day_str
                and r.get("stck_cntg_hour")
                and r["stck_cntg_hour"] not in rows_by_time
            ]
            if not fresh:
                break
            for r in fresh:
                rows_by_time[r["stck_cntg_hour"]] = r
            earliest = min(r["stck_cntg_hour"] for r in fresh)
            if earliest <= _SESSION_START:
                break
            cursor = self._minus_one_minute(earliest)
            await asyncio.sleep(self._delay)

        bars = self.parse_rows(ticker, day, list(rows_by_time.values()))
        if ok and cache:  # 성공 응답만 캐시 (미거래일 = 빈 리스트도 캐시해 재조회 방지)
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(
                [{"t": r["stck_cntg_hour"], "o": r["stck_oprc"], "h": r["stck_hgpr"],
                  "l": r["stck_lwpr"], "c": r["stck_prpr"], "v": r["cntg_vol"]}
                 for r in sorted(rows_by_time.values(), key=lambda r: r["stck_cntg_hour"])]
            ))
        return bars

    async def fetch_days(self, ticker: str, days: list[date]) -> list[Bar]:
        out: list[Bar] = []
        for d in sorted(days):
            out.extend(await self.fetch_day(ticker, d))
        return out

    # -- pure helpers (offline-testable) ----------------------------------

    @staticmethod
    def parse_rows(ticker: str, day: date, rows: list[dict[str, Any]]) -> list[Bar]:
        bars: list[Bar] = []
        for r in sorted(rows, key=lambda r: str(r.get("stck_cntg_hour", ""))):
            hhmmss = str(r.get("stck_cntg_hour", ""))
            if len(hhmmss) != 6:
                continue
            close = Decimal(str(r.get("stck_prpr", "0")))
            vol = Decimal(str(r.get("cntg_vol", "0") or "0"))
            bars.append(
                Bar(
                    ticker=ticker,
                    resolution=Resolution.M1,
                    ts=datetime(day.year, day.month, day.day,
                                int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6]), tzinfo=KST),
                    open=Decimal(str(r.get("stck_oprc", close))),
                    high=Decimal(str(r.get("stck_hgpr", close))),
                    low=Decimal(str(r.get("stck_lwpr", close))),
                    close=close,
                    volume=vol,
                    value=close * vol,
                )
            )
        return bars

    @staticmethod
    def _minus_one_minute(hhmmss: str) -> str:
        t = datetime.strptime(hhmmss, "%H%M%S") - timedelta(minutes=1)
        return t.strftime("%H%M%S")

    def _from_cache(self, ticker: str, day: date, path: Path) -> list[Bar]:
        rows = [
            {"stck_cntg_hour": r["t"], "stck_oprc": r["o"], "stck_hgpr": r["h"],
             "stck_lwpr": r["l"], "stck_prpr": r["c"], "cntg_vol": r["v"],
             "stck_bsop_date": f"{day:%Y%m%d}"}
            for r in json.loads(path.read_text())
        ]
        return self.parse_rows(ticker, day, rows)

    # -- network -----------------------------------------------------------

    async def _get(self, ticker: str, day_str: str, cursor: str) -> dict[str, Any]:
        token = await self._auth.access_token()
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": self._creds.app_key,
            "appsecret": self._creds.app_secret,
            "tr_id": _TR_ID,
            "custtype": "P",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": day_str,
            "FID_INPUT_HOUR_1": cursor,
            "FID_PW_DATA_INCU_YN": "N",
            "FID_FAKE_TICK_INCU_YN": "N",
        }
        return await self._transport("GET", f"{self._base}{_PATH}", headers, params)

    async def _default_transport(
        self, method: str, url: str, headers: dict[str, str], params: dict[str, Any]
    ) -> dict[str, Any]:
        # 모의(vps) 시세 서버는 간헐적 HTTP 5xx를 반환한다(실측) — 백오프 재시도.
        # 시작 시 워밍업 백필이 수십 건을 연속 호출하는 최대 버스트 지점이라, 초당 한도
        # 게이트를 반드시 통과시킨다 (넘기면 계좌 전체가 EGW00201 로 막힌다).
        last_exc: Exception | None = None
        client = shared_client(self._timeout)
        for attempt in range(4):
            try:
                await shared_limiter().acquire()
                resp = await client.get(
                    url, headers=headers, params=params, timeout=self._timeout
                )
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "server error", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                return data
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                await asyncio.sleep(0.8 * (attempt + 1))
        assert last_exc is not None
        raise last_exc


def resample(bars: list[Bar], resolution: Resolution) -> list[Bar]:
    """1분봉 → 상위 분봉(5m/10m/...) 집계. 입력은 시간 오름차순 가정."""
    seconds = resolution.bar_seconds
    if seconds is None:
        raise ValueError(f"{resolution} is not a minute resolution")
    out: list[Bar] = []
    current: Bar | None = None
    bucket_start: datetime | None = None
    for b in bars:
        epoch = int(b.ts.timestamp() // seconds) * seconds
        start = datetime.fromtimestamp(epoch, tz=b.ts.tzinfo)
        if current is None or start != bucket_start or b.ticker != current.ticker:
            if current is not None:
                out.append(current)
            bucket_start = start
            current = Bar(b.ticker, resolution, start, b.open, b.high, b.low, b.close,
                          b.volume, b.value)
        else:
            current = Bar(
                current.ticker, resolution, current.ts, current.open,
                max(current.high, b.high), min(current.low, b.low), b.close,
                current.volume + b.volume, current.value + b.value,
            )
    if current is not None:
        out.append(current)
    return out
