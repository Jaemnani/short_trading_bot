"""EngineHealth — "살아는 있는데 일을 하고 있나" 를 판정하는 상태 집계.

배경: 기존 대시보드는 상태 파일의 갱신 시각만 봐서 프로세스 생존만 알 수 있었다.
그런데 가장 무서운 고장은 프로세스가 멀쩡히 돌면서 **시세를 못 받는** 경우다 —
WS 가 조용히 끊기면 파일은 계속 갱신되고 화면은 정상으로 보이지만 전략은 영원히
관망만 한다. 체결 폴링이 죽은 경우도 같다 (주문은 나가는데 체결을 모른다).

그래서 "마지막으로 실제 일이 일어난 시각" 을 신호로 삼는다:
- 시세: 마지막 봉 수신 시각. 장중 무소식이 길면 이상.
- 체결 폴링: 마지막 성공 시각과 연속 실패 수 (PollGate 가 채운다).

판정은 장 시간을 안다 — 장외의 무소식은 정상이므로 경고하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..execution.poll_gate import KST, PollGate

# 장중 이 시간 넘게 봉이 없으면 시세 이상으로 본다. 1분봉이 기본 해상도라
# 넉넉히 잡아도 5분이면 충분 (거래 없는 종목도 봉은 생성된다).
STALE_FEED_SECONDS = 300.0


@dataclass
class EngineHealth:
    """엔진의 '실제로 일하는 중' 신호. 모든 시각은 KST aware datetime."""

    last_bar_at: datetime | None = None
    last_bar_ticker: str = ""
    bars_received: int = 0
    last_poll_ok_at: datetime | None = None
    poll_failures: int = 0
    feed_connects: int = 0  # WS 재접속 횟수 (많으면 회선 불안정)
    process_errors: int = 0  # 봉 처리 실패 누적 (격리되지만 쌓이면 원인 조사 필요)
    _gate: PollGate = field(default_factory=PollGate)

    def on_bar(self, ticker: str, now: datetime) -> None:
        self.last_bar_at = now.astimezone(KST)
        self.last_bar_ticker = ticker
        self.bars_received += 1

    def on_poll(self, ok: bool, now: datetime) -> None:
        if ok:
            self.last_poll_ok_at = now.astimezone(KST)
            self.poll_failures = 0
        else:
            self.poll_failures += 1

    def on_feed_connect(self) -> None:
        self.feed_connects += 1

    def on_process_error(self) -> None:
        """봉 처리 실패 — 시세 연결은 유지하고 여기 누적해 가시화한다 (조용히 삼키지 않음)."""
        self.process_errors += 1

    def in_session(self, now: datetime) -> bool:
        return self._gate.in_session(now)

    def feed_stale_seconds(self, now: datetime) -> float | None:
        """마지막 봉 이후 경과 초. 봉을 한 번도 못 받았으면 None."""
        if self.last_bar_at is None:
            return None
        return (now.astimezone(KST) - self.last_bar_at).total_seconds()

    def feed_ok(self, now: datetime) -> bool:
        """시세가 건강한가. 장외에는 무소식이 정상이라 항상 True."""
        if not self.in_session(now):
            return True
        stale = self.feed_stale_seconds(now)
        if stale is None:
            return False  # 장중인데 봉을 한 번도 못 받음 = 이상
        return stale <= STALE_FEED_SECONDS

    def snapshot(self, now: datetime) -> dict[str, object]:
        """대시보드용 직렬화. 판정(ok)까지 여기서 내려 화면·알림이 같은 기준을 쓴다."""
        stale = self.feed_stale_seconds(now)
        return {
            "in_session": self.in_session(now),
            "feed_ok": self.feed_ok(now),
            "last_bar_at": self.last_bar_at.isoformat() if self.last_bar_at else None,
            "last_bar_ticker": self.last_bar_ticker or None,
            "feed_stale_seconds": round(stale, 1) if stale is not None else None,
            "bars_received": self.bars_received,
            "feed_connects": self.feed_connects,
            "process_errors": self.process_errors,
            "last_poll_ok_at": (
                self.last_poll_ok_at.isoformat() if self.last_poll_ok_at else None
            ),
            "poll_failures": self.poll_failures,
        }
