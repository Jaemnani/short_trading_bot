"""KIS 거래량순위 조회 (실시간 인기 종목) — 장중 스캐너의 데이터 소스.

/uapi/domestic-stock/v1/quotations/volume-rank (TR FHPST01710000)로 거래량 상위
종목을 당겨와 등락률·거래량증가율·거래대금과 함께 반환한다. 주입식 transport라
요청/파싱이 오프라인 테스트 가능(브로커 어댑터와 동일 패턴).

⚠️ 순위분석 계열 API는 KIS 모의투자 도메인에서 미지원일 수 있다 — 시세 조회는
주문과 무관하므로, 라이브 키가 있으면 라이브 도메인으로 조회하는 것을 권장
(cli가 그렇게 구성한다). 응답 필드명은 포털에서 최종 확인할 것.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..infra.config import KisEnvCreds
from ..infra.http import shared_client
from ..infra.kis_auth import KisAuth
from ..infra.rate_limit import shared_limiter

Transport = Callable[[str, str, dict[str, str], dict[str, Any]], Awaitable[dict[str, Any]]]

_RANK_PATH = "/uapi/domestic-stock/v1/quotations/volume-rank"
_RANK_TR = "FHPST01710000"  # 거래량순위 — ⚠️ verify on portal (모의 미지원 가능)


@dataclass(slots=True)
class RankRow:
    """One ranking row. ``vol_surge``는 전일 대비 거래량증가율(%), ``change_pct``는 등락률(%)."""

    ticker: str
    name: str
    price: float
    change_pct: float
    volume: float  # 누적 거래량
    value_traded: float  # 누적 거래대금 (원)
    vol_surge: float


class KisVolumeRank:
    def __init__(
        self,
        auth: KisAuth,
        creds: KisEnvCreds,
        base_url: str,
        *,
        transport: Transport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._auth = auth
        self._creds = creds
        self._base = base_url.rstrip("/")
        self._transport = transport or self._default_transport
        self._timeout = timeout

    async def top(self, *, market: str = "0000") -> list[RankRow]:
        """거래량 상위 종목 (market: 0000 전체 / 0001 코스피 / 1001 코스닥)."""
        token = await self._auth.access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self._creds.app_key,
            "appsecret": self._creds.app_secret,
            "tr_id": _RANK_TR,
            "custtype": "P",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": market,
            "FID_DIV_CLS_CODE": "0",  # 0 = 전체 (보통주+우선주)
            "FID_BLNG_CLS_CODE": "1",  # 1 = 거래증가율순
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",  # 관리종목 등 제외 플래그
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        }
        resp = await self._transport("GET", f"{self._base}{_RANK_PATH}", headers, params)
        return self.parse(resp)

    @staticmethod
    def parse(resp: dict[str, Any]) -> list[RankRow]:
        out: list[RankRow] = []
        for row in resp.get("output", []):
            ticker = str(row.get("mksc_shrn_iscd", ""))
            if not ticker:
                continue
            out.append(
                RankRow(
                    ticker=ticker,
                    name=str(row.get("hts_kor_isnm", "")),
                    price=_f(row.get("stck_prpr")),
                    change_pct=_f(row.get("prdy_ctrt")),
                    volume=_f(row.get("acml_vol")),
                    value_traded=_f(row.get("acml_tr_pbmn")),
                    vol_surge=_f(row.get("vol_inrt")),
                )
            )
        return out

    async def _default_transport(
        self, method: str, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        # KIS 초당 한도는 계좌 단위로 **모든 엔드포인트 합산**이라 순위 조회도 같은 버킷을
        # 통과해야 한다. 브로커만 제한하면 여기서 새어 체결 조회가 EGW00201 로 밀려난다.
        await shared_limiter().acquire()
        resp = await shared_client(self._timeout).get(
            url, headers=headers, params=payload, timeout=self._timeout
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data


def _f(value: Any) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0
