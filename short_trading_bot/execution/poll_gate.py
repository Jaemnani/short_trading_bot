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

휴장일: KRX 공휴일은 아래 표로 관리한다 (매년 갱신 — 미갱신 연도는 평일로 취급되고
실패 백오프가 60s 로 눌러주므로 안전하게 열화된다).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# KRX 휴장일 — **평일만** 수록한다 (주말은 weekday 검사로 이미 걸러지므로 중복 불필요,
# 오히려 검증 시 혼동을 준다).
#
# ⚠️ 위험 비대칭 — 갱신 시 반드시 인지할 것:
#   · 휴장일을 빠뜨림  → 그날 폴링 시도 → 실패 백오프(60s)로 눌림. **무해**
#   · 거래일을 잘못 넣음 → 그날 체결 폴링이 통째로 멈춤. **위험** (체결 미감지)
#   따라서 확신 없는 날짜는 넣지 말 것. 모르면 빼는 쪽이 안전하다.
#
# 검증법 (반기/연초 갱신 시 실행):
#   FinanceDataReader 로 'KS11' 을 받아 실제 거래일 집합을 만들고, 그 해 평일 집합과
#   차집합을 구하면 지나간 기간의 정답 휴장일이 나온다. 이 표와 대조해
#   "거래일을 잘못 넣은 것" 이 0 인지 확인한다 (2026-08-10 실측으로 이 방식 확인).
KRX_HOLIDAYS: frozenset[date] = frozenset(
    {
        # --- 2026 (1~8월: 실제 거래 데이터로 검증 / 9~12월: 공표 달력 기준) ---
        date(2026, 1, 1),  # 신정 (목)
        date(2026, 2, 16),  # 설날 연휴 (월)
        date(2026, 2, 17),  # (화)
        date(2026, 2, 18),  # (수)
        date(2026, 3, 2),  # 삼일절 대체공휴일 (월)
        date(2026, 5, 1),  # 근로자의 날 (금) — 증시 휴장
        date(2026, 5, 5),  # 어린이날 (화)
        date(2026, 5, 25),  # 부처님오신날 대체공휴일 (월)
        date(2026, 6, 3),  # 지방선거 (수)
        date(2026, 7, 17),  # 제헌절 (금)
        date(2026, 8, 17),  # 광복절 대체공휴일 (월)
        date(2026, 9, 24),  # 추석 연휴 (목)
        date(2026, 9, 25),  # 추석 (금)
        date(2026, 10, 5),  # 개천절 대체공휴일 (월)
        date(2026, 10, 9),  # 한글날 (금)
        date(2026, 12, 25),  # 성탄절 (금)
        date(2026, 12, 31),  # 연말 휴장 (목)
    }
)


@dataclass
class PollGate:
    base_seconds: float = 2.0
    max_backoff_seconds: float = 60.0
    idle_seconds: float = 60.0
    open_minute: int = 8 * 60 + 30  # KST 08:30
    close_minute: int = 16 * 60 + 30  # KST 16:30
    _failures: int = field(default=0, init=False)

    def in_session(self, now: datetime) -> bool:
        """폴링할 가치가 있는 시간대인가 (KST 평일·비휴장일, 세션 창 안)."""
        local = now.astimezone(KST)
        if local.weekday() >= 5 or local.date() in KRX_HOLIDAYS:
            return False
        minute = local.hour * 60 + local.minute
        return self.open_minute <= minute <= self.close_minute

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
