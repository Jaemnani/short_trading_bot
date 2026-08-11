"""KIS REST 호출 속도 제한 (토큰 버킷).

2026-08-11 확정: 장중 500 오류의 사유가 ``EGW00201 "초당 거래건수를 초과하였습니다."`` —
우리 호출이 KIS 초당 한도를 넘고 있었다. 그 500 응답은 ``Connection: close`` 를 달고 와서
커넥션 풀을 깨고, 다음 호출이 DNS 를 다시 조회하며(gaierror 폭주), 그 예외가 봉 처리
경로를 타고 올라와 WS 시세 연결까지 무너뜨렸다. 즉 **한도 초과가 연쇄의 시작점**이다.

평균 호출률은 한도 아래여도 터진다 — 체결 폴링·잔고·주문 코루틴이 같은 순간에 겹치면
순간 초당 건수가 한도를 넘는다. 그래서 평균이 아니라 **버스트를 눌러야** 한다.

대기(queue)가 실패(500)보다 낫다: 체결 조회가 몇십 ms 늦는 것은 무해하지만, 500 은
커넥션·시세 연결까지 끊는다.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    """토큰 버킷. ``rate`` 초당 보충, ``burst`` 최대 적립.

    KIS 공식 한도는 모의투자 초당 2건 / 실전 초당 20건. 기본값은 그보다 보수적으로 잡는다 —
    한도에 딱 맞추면 서버측 계측 오차·재시도가 겹칠 때 다시 초과한다.
    """

    rate: float = 1.5
    burst: float = 2.0
    _tokens: float = field(default=0.0, init=False)
    _updated: float = field(default=0.0, init=False)
    _lock: asyncio.Lock | None = field(default=None, init=False, repr=False)
    waits: int = field(default=0, init=False)  # 대기 발생 횟수 (계측용)

    def __post_init__(self) -> None:
        self._tokens = self.burst
        self._updated = time.monotonic()

    async def acquire(self) -> None:
        """토큰 1개를 얻을 때까지 대기. 호출 순서는 보장하지 않는다(락 경합 순)."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                self.waits += 1
                # 락을 쥔 채로 잠들어 다른 호출도 함께 늦춘다 — 그래야 버스트가 눌린다.
                await asyncio.sleep((1.0 - self._tokens) / self.rate)


_limiters: dict[object, RateLimiter] = {}
# 프로세스 전체 설정. 첫 호출자가 속도를 정하게 두면(초기 구현) 시작 시 백필이 먼저
# 만들어 실전 전환 후에도 모의용 저속이 굳는다 — 시작 시 한 번 명시 설정한다.
_config: tuple[float, float] = (1.5, 2.0)


def configure_shared_limiter(rate: float, burst: float) -> None:
    """실행 모드가 정해진 시점(serve 시작)에 한 번 호출. 이미 만든 버킷에도 반영한다."""
    global _config
    _config = (rate, burst)
    for limiter in _limiters.values():
        limiter.rate, limiter.burst = rate, burst


def shared_limiter() -> RateLimiter:
    """이벤트 루프별 공용 제한기 — 모든 KIS 호출이 같은 버킷을 통과해야 의미가 있다.

    KIS 초당 한도는 **계좌 단위로 전 엔드포인트 합산**이라, 한 곳이라도 버킷 밖에서
    호출하면 그만큼 다른 호출이 EGW00201 로 밀려난다 (2026-08-11 실측).
    """
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = RateLimiter(rate=_config[0], burst=_config[1])
        _limiters[loop] = limiter
    return limiter


def reset_shared_limiter() -> None:
    """테스트용 — 루프별 캐시 제거."""
    _limiters.clear()
