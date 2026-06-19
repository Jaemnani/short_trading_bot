"""Alert notifier abstraction. Backends: in-memory (tests), console, composite.

Telegram/Slack/WebPush backends implement the same ``Notifier`` interface and are added
in their phases (P5 telegram, P10 webpush).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..logging import get_logger


@dataclass(slots=True)
class Notification:
    event: str
    fields: dict[str, Any] = field(default_factory=dict)


class Notifier(ABC):
    @abstractmethod
    async def notify(self, event: str, **fields: Any) -> None: ...


class InMemoryNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def notify(self, event: str, **fields: Any) -> None:
        self.sent.append(Notification(event, fields))


class ConsoleNotifier(Notifier):
    def __init__(self, logger: Any = None) -> None:
        self._log = logger or get_logger("notifier")

    async def notify(self, event: str, **fields: Any) -> None:
        self._log.info(f"notify.{event}", **fields)


class CompositeNotifier(Notifier):
    """Fan out to several notifiers; one failing backend doesn't block the others."""

    def __init__(self, *notifiers: Notifier) -> None:
        self._notifiers = list(notifiers)
        self._log = get_logger("notifier")

    async def notify(self, event: str, **fields: Any) -> None:
        for backend in self._notifiers:
            try:
                await backend.notify(event, **fields)
            except Exception as exc:
                self._log.warning("notify.backend_failed", backend=type(backend).__name__, error=str(exc))
