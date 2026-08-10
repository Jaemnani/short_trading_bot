"""KIS authentication: cached OAuth access token (1-day TTL) + WebSocket approval key.

The access token must be cached and reused — re-issuing too often triggers EGW00201.
The WebSocket ``approval_key`` is obtained separately (``/oauth2/Approval``) and is NOT
the REST Bearer token. Fetchers and the clock are injectable so the caching logic is
unit-testable without any network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import KisEnvCreds
from .http import shared_client

TokenFetcher = Callable[[], Awaitable[tuple[str, int]]]  # -> (access_token, expires_in_seconds)
ApprovalFetcher = Callable[[], Awaitable[str]]
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class _CachedToken:
    value: str
    expires_at: datetime


class KisAuth:
    def __init__(
        self,
        creds: KisEnvCreds,
        base_url: str,
        *,
        token_fetcher: TokenFetcher | None = None,
        approval_fetcher: ApprovalFetcher | None = None,
        clock: Clock = _utcnow,
        skew_seconds: int = 60,
        timeout: float = 10.0,
    ) -> None:
        self._creds = creds
        self._base_url = base_url.rstrip("/")
        self._token_fetcher = token_fetcher or self._default_token_fetch
        self._approval_fetcher = approval_fetcher or self._default_approval_fetch
        self._clock = clock
        self._skew = timedelta(seconds=skew_seconds)
        self._timeout = timeout
        self._token: _CachedToken | None = None
        self._approval_key: str | None = None

    async def access_token(self) -> str:
        now = self._clock()
        if self._token is not None and self._token.expires_at - self._skew > now:
            return self._token.value
        value, expires_in = await self._token_fetcher()
        self._token = _CachedToken(value=value, expires_at=now + timedelta(seconds=expires_in))
        return value

    async def approval_key(self) -> str:
        if self._approval_key is None:
            self._approval_key = await self._approval_fetcher()
        return self._approval_key

    def invalidate(self) -> None:
        """Drop the cached token (e.g. after an auth error) to force re-issue."""
        self._token = None

    # -- default network fetchers ---------------------------------------

    async def _default_token_fetch(self) -> tuple[str, int]:
        # 공용 클라이언트 — 호출당 DNS/TLS 재수립 방지 (infra/http.py 참조).
        resp = await shared_client(self._timeout).post(
            f"{self._base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self._creds.app_key,
                "appsecret": self._creds.app_secret,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"], int(data.get("expires_in", 86400))

    async def _default_approval_fetch(self) -> str:
        resp = await shared_client(self._timeout).post(
            f"{self._base_url}/oauth2/Approval",
            json={
                "grant_type": "client_credentials",
                "appkey": self._creds.app_key,
                "secretkey": self._creds.app_secret,  # NOTE: 'secretkey', not 'appsecret'
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data["approval_key"])
