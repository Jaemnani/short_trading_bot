"""Discord webhook notifier.

Setup: Discord server > 채널 편집 > 연동(Integrations) > 웹후크(Webhooks) > 새 웹후크 >
URL 복사 -> STB_NOTIFIER__DISCORD_WEBHOOK_URL. No bot token needed.

The HTTP transport is injectable for offline tests. Failures are raised to the caller;
CompositeNotifier isolates them so a Discord outage never blocks trading.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .base import Notifier

Transport = Callable[[str, dict[str, Any]], Awaitable[None]]

_MAX_CONTENT = 2000  # Discord hard limit


class DiscordNotifier(Notifier):
    def __init__(
        self,
        webhook_url: str,
        *,
        username: str = "short_trading_bot",
        transport: Transport | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._url = webhook_url
        self._username = username
        self._transport = transport or self._default_transport
        self._timeout = timeout

    async def notify(self, event: str, **fields: Any) -> None:
        await self._transport(self._url, {"content": self.format(event, fields), "username": self._username})

    @staticmethod
    def format(event: str, fields: dict[str, Any]) -> str:
        lines = [f"**{event}**"]
        lines.extend(f"· {key}: {value}" for key, value in fields.items())
        return "\n".join(lines)[:_MAX_CONTENT]

    async def _default_transport(self, url: str, payload: dict[str, Any]) -> None:
        # 웹후크 URL 자체가 자격증명(<id>/<token>)이다. httpx 예외 메시지(raise_for_status 등)는
        # URL 전체를 담아 로그에 남기므로, URL 없는 메시지로 바꿔 올린다 (#12).
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                raise RuntimeError(f"discord request failed: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise RuntimeError(f"discord HTTP {resp.status_code}")
