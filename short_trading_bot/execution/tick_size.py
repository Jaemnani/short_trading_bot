"""KRX 호가단위(tick size) — 주문 가격을 유효한 호가로 맞춘다.

2026-08-11 실측: 호가단위에 안 맞는 가격으로 주문하면 KIS 가
``모의투자 주문처리가 안되었습니다(호가단위 오류)`` 로 거부한다.

봇의 정상 경로는 시세(마지막 체결가)를 그대로 지정가로 쓰므로 대개 유효하다. 문제는
**시세를 아직 못 받았을 때의 폴백(평단가)** — 평단가는 체결들의 가중평균이라 소수점이
붙고, 그대로 주문하면 거부된다. 손절처럼 반드시 나가야 하는 주문이 이걸로 막히면
치명적이라 방어한다.

KRX 규칙 (2023-01 개편):
    2,000 미만        1원     |  2,000~5,000     5원
    5,000~20,000     10원     |  20,000~50,000  50원
    50,000~200,000  100원     |  200,000~500,000 500원
    500,000 이상   1,000원
ETF/ETN 은 전 구간 5원. 2,000원 이상 구간의 주식 호가단위는 모두 5의 배수라 주식 기준으로
맞추면 ETF 에도 유효하다. 2,000원 미만만 주식(1원)이 ETF(5원)보다 촘촘하므로 5원으로 맞춘다.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from ..domain.enums import Side

_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("2000"), Decimal("5")),  # 2,000 미만 — ETF(5원)까지 안전하게 5로
    (Decimal("5000"), Decimal("5")),
    (Decimal("20000"), Decimal("10")),
    (Decimal("50000"), Decimal("50")),
    (Decimal("200000"), Decimal("100")),
    (Decimal("500000"), Decimal("500")),
)
_TOP_TICK = Decimal("1000")


def tick_size(price: Decimal) -> Decimal:
    """해당 가격대의 호가단위 (주식·ETF 모두에 유효한 보수적 값)."""
    for upper, tick in _BANDS:
        if price < upper:
            return tick
    return _TOP_TICK


def round_to_tick(price: Decimal, side: Side) -> Decimal:
    """가격을 유효 호가로 정렬한다.

    체결 가능성이 유리한 쪽으로 붙인다 — 매수는 올림(조금 비싸게 사서 체결↑),
    매도는 내림(조금 싸게 팔아 체결↑). 손절이 호가 정렬 때문에 미체결로 남는 것보다
    한 틱 불리한 게 낫다.
    """
    tick = tick_size(price)
    rounding = ROUND_UP if side is Side.BUY else ROUND_DOWN
    aligned = (price / tick).quantize(Decimal("1"), rounding=rounding) * tick
    return max(aligned, tick)  # 0원 주문 방지
