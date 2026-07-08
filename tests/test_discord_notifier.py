from typing import Any

from short_trading_bot.infra.config import Settings
from short_trading_bot.infra.notifier.base import CompositeNotifier
from short_trading_bot.infra.notifier.discord import DiscordNotifier
from short_trading_bot.infra.notifier.factory import build_notifier


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, payload: dict[str, Any]) -> None:
        self.calls.append((url, payload))


async def test_discord_posts_formatted_message() -> None:
    transport = FakeTransport()
    n = DiscordNotifier("https://discord.com/api/webhooks/x/y", transport=transport)
    await n.notify("order.accepted", ticker="005930", side="BUY", qty="10")

    url, payload = transport.calls[0]
    assert url.startswith("https://discord.com/api/webhooks/")
    assert payload["username"] == "short_trading_bot"
    assert payload["content"].startswith("**order.accepted**")
    assert "· ticker: 005930" in payload["content"]
    assert "· qty: 10" in payload["content"]


def test_discord_content_truncated_to_limit() -> None:
    content = DiscordNotifier.format("event", {"big": "x" * 5000})
    assert len(content) <= 2000


def test_build_notifier_console_only_without_webhook() -> None:
    notifier = build_notifier(Settings(_env_file=None))
    assert isinstance(notifier, CompositeNotifier)
    assert len(notifier._notifiers) == 1  # console only


def test_build_notifier_adds_discord_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("STB_NOTIFIER__DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/x/y")
    notifier = build_notifier(Settings(_env_file=None))
    assert any(isinstance(b, DiscordNotifier) for b in notifier._notifiers)  # type: ignore[attr-defined]
