"""RateLimiter — KIS 초당 한도 준수 검증.

2026-08-11 확정 사고: EGW00201 "초당 거래건수를 초과하였습니다." 가 500 의 사유였고,
그 500 이 Connection: close 를 달고 와 커넥션 풀·DNS·WS 시세 연결까지 연쇄로 무너뜨렸다.
평균 호출률이 한도 아래여도 코루틴이 겹치면 순간 버스트가 한도를 넘는다.
"""

import asyncio
import time

from short_trading_bot.infra.rate_limit import (
    RateLimiter,
    configure_shared_limiter,
    reset_shared_limiter,
    shared_limiter,
)


async def test_burst_is_capped() -> None:
    """버스트 한도까지는 즉시 통과하고, 그 다음부터 대기가 걸려야 한다."""
    lim = RateLimiter(rate=10.0, burst=3.0)
    t0 = time.monotonic()
    for _ in range(3):
        await lim.acquire()
    assert time.monotonic() - t0 < 0.05  # 적립분은 즉시
    await lim.acquire()  # 4번째는 보충을 기다린다
    assert time.monotonic() - t0 >= 0.08  # ~1/10초


async def test_sustained_rate_respected() -> None:
    """지속 호출은 설정 rate 를 넘지 않아야 한다 (한도 초과 = 500)."""
    rate = 20.0
    lim = RateLimiter(rate=rate, burst=1.0)
    n = 10
    t0 = time.monotonic()
    for _ in range(n):
        await lim.acquire()
    elapsed = time.monotonic() - t0
    observed = n / max(elapsed, 1e-9)
    assert observed <= rate * 1.35  # 여유를 둬도 rate 근처를 넘지 않는다


async def test_concurrent_callers_share_budget() -> None:
    """동시 코루틴이 각자 버킷을 쓰면 의미가 없다 — 합산이 한도를 지켜야 한다."""
    rate = 20.0
    lim = RateLimiter(rate=rate, burst=2.0)

    async def worker() -> None:
        for _ in range(5):
            await lim.acquire()

    t0 = time.monotonic()
    await asyncio.gather(*(worker() for _ in range(4)))  # 총 20회
    elapsed = time.monotonic() - t0
    assert 20 / max(elapsed, 1e-9) <= rate * 1.35


async def test_waits_are_counted() -> None:
    lim = RateLimiter(rate=50.0, burst=1.0)
    await lim.acquire()  # 적립분 소진
    await lim.acquire()  # 대기 발생
    assert lim.waits >= 1


async def test_shared_limiter_is_single_bucket() -> None:
    reset_shared_limiter()
    a = shared_limiter()
    b = shared_limiter()
    assert a is b  # 모든 KIS 호출이 같은 버킷을 통과해야 한다
    reset_shared_limiter()


async def test_configure_applies_to_existing_bucket() -> None:
    """첫 호출자가 속도를 정하면 시작 시 백필이 먼저 만들어 실전 속도가 안 먹는다 —
    설정은 언제 호출해도 이미 만든 버킷에 반영돼야 한다."""
    reset_shared_limiter()
    lim = shared_limiter()
    configure_shared_limiter(12.0, 15.0)
    assert (lim.rate, lim.burst) == (12.0, 15.0)
    configure_shared_limiter(1.5, 2.0)  # 기본값 복구 (다른 테스트 오염 방지)
    reset_shared_limiter()


async def test_configure_applies_to_new_bucket() -> None:
    reset_shared_limiter()
    configure_shared_limiter(9.0, 9.0)
    assert shared_limiter().rate == 9.0
    configure_shared_limiter(1.5, 2.0)
    reset_shared_limiter()
