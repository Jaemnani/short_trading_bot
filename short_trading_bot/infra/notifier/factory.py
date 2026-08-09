"""Assemble the notifier stack from settings: console always, Kakao/Discord when configured."""

from __future__ import annotations

from ..config import Settings
from .base import CompositeNotifier, ConsoleNotifier, Notifier
from .discord import DiscordNotifier
from .kakao import KakaoNotifier


def build_notifier(settings: Settings) -> Notifier:
    backends: list[Notifier] = [ConsoleNotifier()]
    if settings.notifier.kakao_rest_api_key:
        backends.append(
            KakaoNotifier(
                settings.notifier.kakao_rest_api_key,
                settings.notifier.kakao_token_path,
                client_secret=settings.notifier.kakao_client_secret,
            )
        )
    if settings.notifier.discord_webhook_url:
        backends.append(DiscordNotifier(settings.notifier.discord_webhook_url))
    return CompositeNotifier(*backends)
