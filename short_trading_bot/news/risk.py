"""DART 위험공시 체커 — 급등의 '이유'가 위험 공시인 종목을 스캐너에서 거른다.

유상증자·CB·감자·관리종목 같은 공시로 급등/급락하는 종목은 모멘텀 합류 대상이
아니다 (되돌림·거래정지 리스크). 종목코드→DART corp_code 매핑은 corpCode.xml을
한 번 받아 디스크에 캐시하고, 최근 N일 공시 목록(list.json)에서 위험 키워드를
찾는다. transport 주입으로 오프라인 테스트 가능 (리포 표준 패턴).
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx

from ..infra.logging import get_logger

# (url, params) -> 응답 바이트 (list.json은 json, corpCode.xml은 zip)
Transport = Callable[[str, dict[str, str]], Awaitable[bytes]]

_BASE = "https://opendart.fss.or.kr/api"

RISK_KEYWORDS = (
    "유상증자", "무상감자", "감자결정", "전환사채", "신주인수권부사채", "교환사채",
    "관리종목", "불성실공시", "매매거래정지", "상장폐지", "횡령", "배임",
    "감사의견", "회생절차", "파산",
)


class DartRiskChecker:
    def __init__(
        self,
        api_key: str,
        *,
        cache_dir: str | Path = "data",
        transport: Transport | None = None,
        lookback_days: int = 7,
    ) -> None:
        self._key = api_key
        self._cache = Path(cache_dir) / "dart_corp_codes.json"
        self._transport = transport or self._default_transport
        self._days = lookback_days
        self._map: dict[str, str] | None = None  # stock_code -> corp_code
        self._log = get_logger("dart_risk")

    async def risk_filings(self, stock_code: str, *, on: date | None = None) -> list[str]:
        """최근 lookback_days일 위험 공시 제목들 (없으면 빈 리스트). 실패 시 빈 리스트
        (공시 확인 불가로 매매를 막지는 않는다 — 필터는 보수적 추가 장치)."""
        try:
            corp = await self._corp_code(stock_code)
            if corp is None:
                return []
            end = on or date.today()
            begin = end - timedelta(days=self._days)
            raw = await self._transport(f"{_BASE}/list.json", {
                "crtfc_key": self._key, "corp_code": corp,
                "bgn_de": f"{begin:%Y%m%d}", "end_de": f"{end:%Y%m%d}",
                "page_count": "100",
            })
            data: dict[str, Any] = json.loads(raw)
            if str(data.get("status")) != "000":
                return []
            return [
                str(r.get("report_nm", ""))
                for r in data.get("list", [])
                if any(k in str(r.get("report_nm", "")) for k in RISK_KEYWORDS)
            ]
        except Exception:
            self._log.warning("dart_risk.check_failed", ticker=stock_code)
            return []

    async def is_risky(self, stock_code: str, *, on: date | None = None) -> bool:
        return bool(await self.risk_filings(stock_code, on=on))

    # -- corp_code map ------------------------------------------------------

    async def _corp_code(self, stock_code: str) -> str | None:
        if self._map is None:
            if self._cache.exists():
                self._map = json.loads(self._cache.read_text())
            else:
                raw = await self._transport(f"{_BASE}/corpCode.xml", {"crtfc_key": self._key})
                self._map = self.parse_corp_codes(raw)
                self._cache.parent.mkdir(parents=True, exist_ok=True)
                self._cache.write_text(json.dumps(self._map))
                self._log.info("dart_risk.corp_map_cached", entries=len(self._map))
        return self._map.get(stock_code)

    @staticmethod
    def parse_corp_codes(zip_bytes: bytes) -> dict[str, str]:
        """corpCode.xml(zip) → {종목코드: corp_code} (상장사만)."""
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            xml = zf.read(zf.namelist()[0])
        out: dict[str, str] = {}
        for el in ElementTree.fromstring(xml).iter("list"):
            stock = (el.findtext("stock_code") or "").strip()
            corp = (el.findtext("corp_code") or "").strip()
            if stock and corp:
                out[stock] = corp
        return out

    # -- network ------------------------------------------------------------

    @staticmethod
    async def _default_transport(url: str, params: dict[str, str]) -> bytes:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.content
