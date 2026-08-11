"""라이브 기본기능 자가점검 — 매수·매도·취소·조회가 '실제로' 되는지 미리 확인한다.

배경 (2026-08-11): 실주문 전환(08-03) 이후 **매도가 100% 거부**되고 있었는데
(SLL_TYPE="01" → IGW00007), 손절 신호가 실제로 뜬 08-11 아침에야 발각됐다. 그 사이
봇은 "포지션을 청산할 수 없는 상태"로 8일간 운용됐다. 기회가 왔을 때 알아채는 구조는
너무 늦다 — 기본기능은 미리, 반복적으로 확인해야 한다.

설계 원칙:
- **비파괴**: 주문은 체결 불가 지정가(매수는 시장가 대비 -10%, 매도는 +10%) 1주로 넣고
  즉시 취소한다. 잔고·포지션을 바꾸지 않는다.
- **거부 사유를 구분**: '전문 형식 오류'(IGW00007/MCA)는 실패지만, '예수금/수량 부족'은
  API 계약이 정상이라는 뜻이므로 통과로 본다. 보유가 없어도 매도 경로를 검증할 수 있다.
- **한도 준수**: 모든 호출이 공용 RateLimiter 를 통과한다 (EGW00201 방지).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

from ..domain.enums import Market, Side
from ..execution.types import OrderRequest

# 이 문자열이 거부 사유에 있으면 '전문 형식' 문제 = 진짜 실패.
_FORMAT_ERRORS = ("IGW00007", "MCA", "전문")
# 이 사유들은 API 계약이 정상이라는 증거 (돈/수량이 없을 뿐).
_BUSINESS_REJECTS = ("잔고", "수량", "예수금", "부족", "매도가능", "주문가능")


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str

    @property
    def mark(self) -> str:
        return "OK" if self.ok else "!!"


def classify_reject(reason: str) -> tuple[bool, str]:
    """주문 거부 사유 판정 → (통과 여부, 설명).

    형식 오류는 실패, 잔고/수량 부족은 통과(계약은 정상), 나머지는 보수적으로 실패.
    """
    text = reason or ""
    if any(token in text for token in _FORMAT_ERRORS):
        return False, f"전문 형식 오류 — {text}"
    if any(token in text for token in _BUSINESS_REJECTS):
        return True, f"형식 정상 (잔고/수량 사유로 거부: {text})"
    return False, f"거부: {text}"


async def _order_roundtrip(
    broker: Any, ticker: str, side: Side, price: Decimal
) -> tuple[bool, str]:
    """체결 불가 지정가로 주문 → (수용 시) 즉시 취소. 잔고를 바꾸지 않는다."""
    req = OrderRequest(
        client_order_id=f"selfcheck-{uuid4().hex[:8]}",
        lot_id="selfcheck",
        ticker=ticker,
        market=Market.KRX,
        side=side,
        qty=Decimal("1"),
        price=price,
        ord_dvsn="00",
    )
    ack = await broker.submit_order(req)
    if not ack.accepted:
        return classify_reject(ack.reject_reason or "")
    if not ack.broker_order_no:
        return False, "수용됐으나 주문번호 없음 (응답 파싱 확인 필요)"
    cancel = await broker.cancel_order(req, ack.broker_order_no)
    if not cancel.accepted:
        return False, f"주문 수용됐으나 취소 실패 — {cancel.reject_reason} (미체결 주문 잔존!)"
    return True, f"주문 수용({ack.broker_order_no}) + 취소 정상"
