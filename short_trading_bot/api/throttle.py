"""로그인 실패 제한 — 대시보드 비밀번호 무차별 대입 방지 (프로세스 메모리)."""

from __future__ import annotations

import time
from collections import deque


class LoginThrottle:
    """클라이언트(IP)별 로그인 실패 횟수 제한 — 무차별 대입 방지 (in-memory)."""

    def __init__(self, max_failures: int = 10, window_seconds: float = 300.0) -> None:
        self._max = max_failures
        self._window = window_seconds
        self._fails: dict[str, deque[float]] = {}

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._fails.setdefault(key, deque())
        while q and now - q[0] > self._window:
            q.popleft()
        return q

    def blocked(self, key: str, *, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        return len(self._recent(key, t)) >= self._max

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        self._recent(key, t).append(t)

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)
