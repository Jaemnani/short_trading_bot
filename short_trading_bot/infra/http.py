"""프로세스 공용 httpx.AsyncClient — 연결·DNS 재사용.

배경 (2026-08-10 사고): 모든 REST 호출이 ``async with httpx.AsyncClient(...)`` 로 매번
새 클라이언트를 만들고 있었다. 클라이언트마다 새 커넥션이므로 **호출 1회 = DNS 조회 1회
+ TCP 핸드셰이크 + TLS 핸드셰이크** 다.

실측 호출량: 체결 폴링 2초 주기(장중 ~11,700회) + 상태 스냅샷의 잔고 조회 5초 주기
(24시간 ~17,280회) 등 하루 약 29,000회. 그 결과 macOS 리졸버가 간헐 실패
(``socket.gaierror: nodename nor servname provided``) 했고, 그 예외가 봉 처리 경로를 타고
올라와 **WS 시세 연결까지 끊었다** — 하루 재접속 219회 중 161회가 이 DNS 실패였다.

공용 클라이언트는 keep-alive 커넥션 풀을 유지하므로 DNS·핸드셰이크가 커넥션 단위로만
일어난다 (호출당 → 커넥션당). 지연도 함께 줄어든다.

이벤트 루프별로 보관한다 — 다른 루프에서 만든 클라이언트를 재사용하면 httpx 내부
동기화가 깨진다 (CLI 명령이 asyncio.run 을 여러 번 호출하는 경우 대비).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

_clients: dict[Any, httpx.AsyncClient] = {}

# 기본 커넥션 풀. KIS 도메인 몇 개만 쓰므로 크게 잡을 필요가 없다.
_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=60.0)


def shared_client(timeout: float = 10.0) -> httpx.AsyncClient:
    """현재 이벤트 루프의 공용 클라이언트. 호출별 타임아웃은 요청 인자로 덮어쓸 것."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout, limits=_LIMITS)
        _clients[loop] = client
    return client


async def close_shared_client() -> None:
    """종료 시 정리 (미호출이어도 프로세스 종료로 회수되지만 경고가 남는다)."""
    loop = asyncio.get_running_loop()
    client = _clients.pop(loop, None)
    if client is not None and not client.is_closed:
        await client.aclose()
