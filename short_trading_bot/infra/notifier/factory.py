"""Assemble the notifier stack from settings: console always, Discord when configured."""

from __future__ import annotations

from ..config import Settings
from .base import CompositeNotifier, ConsoleNotifier, Notifier
from .discord import DiscordNotifier


def build_notifier(settings: Settings) -> Notifier:
    backends: list[Notifier] = [ConsoleNotifier()]
    if settings.notifier.discord_webhook_url:
        backends.append(DiscordNotifier(settings.notifier.discord_webhook_url))
    return CompositeNotifier(*backends)
