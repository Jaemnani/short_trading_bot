"""카카오톡 "나에게 보내기" notifier — 내 카카오톡 '나와의 채팅'으로 알림 발송.

왜 카카오인가: 모바일에서 상시 보는 채널 (Discord 는 유저가 모바일 미사용 — 2026-08-08).
무료·개인용. KIS 봇의 이상/매매/요약 이벤트를 전부 이 채널로 보낸다.

API 사실관계 (2026-08 확인):
- 엔드포인트 ``POST kapi.kakao.com/v2/api/talk/memo/default/send`` (scope: talk_message).
- text 템플릿의 본문 한도 200자, ``link`` 필드 누락 시 400/-2 로 거부
  (2026-07 devtalk 사례 — link 는 필수).
- 액세스 토큰 6시간 / 리프레시 토큰 2개월 (잔여 1개월 미만일 때 갱신 응답에 새
  리프레시 토큰 동봉). 봇이 매 발송 전 만료 여부를 보고 자동 갱신 + 파일에 영속하므로,
  하루 1건(일일 요약)만 나가도 토큰은 영구 유지된다. 봇을 2개월+ 완전 정지하면
  리프레시 토큰이 죽으므로 ``trader kakao-auth`` 재승인 필요.

발송 폭주 가드: 분당 상한(기본 10건)을 넘는 알림은 버린다 (콘솔/로그에는 전부 남음 —
카카오 쿼터 보호 + 장중 이벤트 폭주 시 폰 알림 지옥 방지).
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..logging import get_logger
from .base import Notifier

AUTH_HOST = "https://kauth.kakao.com"
API_HOST = "https://kapi.kakao.com"
_TEXT_LIMIT = 200  # 카카오 text 템플릿 본문 하드 한도
_REFRESH_MARGIN_SECONDS = 600  # 만료 10분 전부터 미리 갱신

# (url, headers, form_data) -> 응답 JSON. 토큰 갱신·발송 모두 form POST 라 시그니처 공유.
Transport = Callable[[str, dict[str, str], dict[str, str]], Awaitable[dict[str, Any]]]


@dataclass
class KakaoToken:
    access_token: str
    refresh_token: str
    access_expires_at: float  # epoch seconds

    def needs_refresh(self, now: float) -> bool:
        return now >= self.access_expires_at - _REFRESH_MARGIN_SECONDS

    @staticmethod
    def load(path: Path) -> KakaoToken | None:
        try:
            raw = json.loads(path.read_text())
            return KakaoToken(
                access_token=str(raw["access_token"]),
                refresh_token=str(raw["refresh_token"]),
                access_expires_at=float(raw["access_expires_at"]),
            )
        except (OSError, KeyError, ValueError):
            return None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "access_expires_at": self.access_expires_at,
                }
            )
        )
        tmp.replace(path)  # 원자적 교체 — 갱신 중 크래시로 토큰 파일이 깨지지 않게


def apply_token_response(
    prev_refresh_token: str, payload: dict[str, Any], now: float
) -> KakaoToken:
    """kauth 토큰 응답(최초 발급/갱신 공용)을 KakaoToken 으로.

    갱신 응답은 리프레시 토큰 잔여 1개월 미만일 때만 새 refresh_token 을 동봉한다 —
    없으면 기존 것을 유지해야 한다 (없다고 비우면 다음 갱신부터 영구 실패).
    """
    return KakaoToken(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token") or prev_refresh_token),
        access_expires_at=now + float(payload["expires_in"]),
    )


async def http_post_form(
    url: str, headers: dict[str, str], data: dict[str, str], *, timeout_seconds: float = 5.0
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.post(url, headers=headers, data=data)
        if resp.status_code >= 400:
            # 카카오는 진짜 사유를 본문 JSON(error_code=KOE010 등)에만 담는다 —
            # raise_for_status 만 쓰면 "401" 만 남아 진단 불가 (2026-08-09 KOE010 사고).
            raise RuntimeError(f"kakao HTTP {resp.status_code}: {resp.text[:300]}")
        body: dict[str, Any] = resp.json()
        return body


class KakaoNotifier(Notifier):
    def __init__(
        self,
        rest_api_key: str,
        token_path: str | Path,
        *,
        client_secret: str = "",
        transport: Transport | None = None,
        timeout: float = 5.0,
        max_per_minute: int = 10,
        link_url: str = "http://localhost:8000",  # text 템플릿 필수 link — 대시보드로
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = rest_api_key
        self._client_secret = client_secret
        self._path = Path(token_path)
        self._transport = transport or self._default_transport
        self._timeout = timeout
        self._max_per_minute = max_per_minute
        self._link_url = link_url
        self._clock = clock
        self._token: KakaoToken | None = None
        self._sent_at: list[float] = []  # 최근 발송 시각 (폭주 가드)
        self._log = get_logger("kakao_notifier")

    async def notify(self, event: str, **fields: Any) -> None:
        now = self._clock()
        if not self._allow(now):
            self._log.warning("kakao.throttled", dropped=event)
            return
        token = await self._fresh_token(now)
        template = {
            "object_type": "text",
            "text": self.format(event, fields),
            "link": {"web_url": self._link_url, "mobile_web_url": self._link_url},
        }
        resp = await self._transport(
            f"{API_HOST}/v2/api/talk/memo/default/send",
            {"Authorization": f"Bearer {token.access_token}"},
            {"template_object": json.dumps(template, ensure_ascii=False)},
        )
        if resp.get("result_code") != 0:
            raise RuntimeError(f"kakao send failed: {resp}")
        self._sent_at.append(now)

    @staticmethod
    def format(event: str, fields: dict[str, Any]) -> str:
        lines = [f"[STB] {event}"]
        lines.extend(f"· {key}: {value}" for key, value in fields.items())
        return "\n".join(lines)[:_TEXT_LIMIT]

    def _allow(self, now: float) -> bool:
        self._sent_at = [t for t in self._sent_at if now - t < 60.0]
        return len(self._sent_at) < self._max_per_minute

    async def _fresh_token(self, now: float) -> KakaoToken:
        if self._token is None:
            self._token = KakaoToken.load(self._path)
        if self._token is None:
            raise RuntimeError(
                f"kakao token file not found: {self._path} — run `trader kakao-auth` first"
            )
        if self._token.needs_refresh(now):
            form = {
                "grant_type": "refresh_token",
                "client_id": self._key,
                "refresh_token": self._token.refresh_token,
            }
            if self._client_secret:  # 앱 보안 설정이 '사용함' 이면 갱신에도 필수
                form["client_secret"] = self._client_secret
            payload = await self._transport(f"{AUTH_HOST}/oauth/token", {}, form)
            self._token = apply_token_response(self._token.refresh_token, payload, now)
            self._token.save(self._path)
            self._log.info("kakao.token_refreshed")
        return self._token

    async def _default_transport(
        self, url: str, headers: dict[str, str], data: dict[str, str]
    ) -> dict[str, Any]:
        return await http_post_form(url, headers, data, timeout_seconds=self._timeout)


def extract_auth_code(value: str) -> str:
    """인가 코드 또는 리다이렉트 URL 전체를 받아 코드만 돌려준다.

    승인 후 브라우저 주소창을 통째로 복사하는 게 사람에게 가장 쉬운 동작이라
    (localhost 수신 서버가 없으면 '연결할 수 없음' 페이지가 뜨지만 URL 에는 code 가 남는다)
    URL·코드 양쪽을 모두 받는다.
    """
    text = value.strip().strip("\"'")
    if "code=" not in text:
        return text
    from urllib.parse import parse_qs, urlparse

    query = urlparse(text).query or text.split("?", 1)[-1]
    codes = parse_qs(query).get("code")
    return codes[0] if codes else text


async def exchange_auth_code(
    rest_api_key: str,
    redirect_uri: str,
    code: str,
    *,
    client_secret: str = "",
    transport: Transport | None = None,
) -> KakaoToken:
    """최초 1회: 브라우저 승인으로 받은 인가 코드를 토큰으로 교환 (kakao-auth CLI 용)."""
    send = transport or http_post_form
    form = {
        "grant_type": "authorization_code",
        "client_id": rest_api_key,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    if client_secret:  # 앱 보안 설정이 '사용함' 이면 없을 때 KOE010
        form["client_secret"] = client_secret
    payload = await send(f"{AUTH_HOST}/oauth/token", {}, form)
    if "access_token" not in payload:
        raise RuntimeError(f"kakao auth failed: {payload}")
    return apply_token_response("", payload, time.time())
