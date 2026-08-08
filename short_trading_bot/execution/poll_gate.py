"""PollGate — 체결 폴링의 시점·간격 결정 (장 세션 게이트 + 연속 실패 백오프).

배경 (2026-08-08): KIS 모의(VTS) 도메인은 장외 시간·주말에 국내 체결내역 TR
(inquire-daily-ccld)을 500으로 거부한다 — 관측된 fill_poll.error 699건 전부 장외
(금 20:00 이후~토요일), 장중 0건. 체결은 정규장에서만 발생하므로 장외 폴링은 정보
이득이 0이고 에러 로그·API 호출만 쌓는다.

두 겹의 안정 장치 (둘 다 순수 로직 — 단위 테스트 대상):
- 세션 창: KST 평일 08:30~16:30 밖에서는 폴링을 쉰다. 정규장(09:00~15:30) 앞뒤 여유는
  개장 직후 ack·마감 직전 주문의 지연 체결을 놓치지 않기 위한 마진.
- 백오프: 창 안이라도 연속 실패 시 간격을 2배씩 늘려(상한 60s) KIS 장애 해머링을 피하고,
  성공 즉시 기본 주기로 복귀한다.

한계: 공휴일은 평일로 간주되어 폴링이 시도되지만, 실패 백오프가 60s 간격으로 눌러준다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


@dataclass
class PollGate:
    base_seconds: float = 2.0
    max_backoff_seconds: float = 60.0
    idle_seconds: float = 60.0
    open_minute: int = 8 * 60 + 30  # KST 08:30
    close_minute: int = 16 * 60 + 30  # KST 16:30
    _failures: int = field(default=0, init=False)

    def in_session(self, now: datetime) -> bool:
        """폴링할 가치가 있는 시간대인가 (KST 평일, 세션 창 안)."""
        local = now.astimezone(KST)
        minute = local.hour * 60 + local.minute
        return local.weekday() < 5 and self.open_minute <= minute <= self.close_minute

    def record(self, ok: bool) -> None:
        """이번 폴링 결과를 반영 — 성공은 백오프 리셋, 실패는 누적."""
        self._failures = 0 if ok else self._failures + 1

    def next_delay(self, now: datetime) -> float:
        """다음 폴링까지 대기 시간(초)."""
        if not self.in_session(now):
            return self.idle_seconds
        if self._failures == 0:
            return self.base_seconds
        return min(self.base_seconds * (2.0**self._failures), self.max_backoff_seconds)

    @property
    def consecutive_failures(self) -> int:
        return self._failures
