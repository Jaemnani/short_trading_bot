"""공용 httpx 클라이언트 — 커넥션·DNS 재사용의 근거.

2026-08-10 사고: 호출마다 새 AsyncClient 를 만들어 하루 약 29,000회의 DNS 조회가
발생했고, macOS 리졸버가 간헐 실패(socket.gaierror)하며 그 예외가 봉 처리 경로를 타고
올라와 WS 시세 연결까지 끊었다 (재접속 219회 중 161회가 이 원인).
"""

import asyncio

from short_trading_bot.infra.http import close_shared_client, shared_client


async def test_same_loop_returns_same_client() -> None:
    a = shared_client()
    b = shared_client()
    assert a is b  # 재사용되어야 커넥션 풀·DNS 캐시가 유지된다
    await close_shared_client()


async def test_close_then_new_client() -> None:
    a = shared_client()
    await close_shared_client()
    b = shared_client()
    assert b is not a and not b.is_closed
    await close_shared_client()


async def test_closed_client_is_replaced() -> None:
    a = shared_client()
    await a.aclose()  # 외부에서 닫힌 경우에도 다음 호출이 살아있는 클라이언트를 줘야 한다
    b = shared_client()
    assert b is not a and not b.is_closed
    await close_shared_client()


def test_separate_loops_get_separate_clients() -> None:
    """다른 이벤트 루프의 클라이언트를 재사용하면 httpx 내부 동기화가 깨진다."""
    ids: list[int] = []

    async def grab() -> None:
        ids.append(id(shared_client()))
        await close_shared_client()

    asyncio.run(grab())
    asyncio.run(grab())
    assert len(ids) == 2  # 각 루프가 자기 클라이언트를 만들고 정리한다


async def test_close_is_idempotent() -> None:
    shared_client()
    await close_shared_client()
    await close_shared_client()  # 두 번 닫아도 예외 없어야 (종료 경로에서 중복 호출 가능)
