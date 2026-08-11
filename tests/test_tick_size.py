"""KRX 호가단위 정렬 — "호가단위 오류" 거부 방지.

2026-08-11 실측: 호가에 안 맞는 가격은 KIS 가
`모의투자 주문처리가 안되었습니다(호가단위 오류)` 로 거부한다. 정상 경로는 시세를 그대로
쓰지만, 시세 미수신 시 폴백인 평단가는 체결 가중평균이라 소수점이 붙는다 — 손절이 이걸로
막히면 치명적이다.
"""

from decimal import Decimal

import pytest

from short_trading_bot.domain.enums import Side
from short_trading_bot.execution.tick_size import round_to_tick, tick_size


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("1500", "5"),  # 2,000 미만도 5 — ETF(전 구간 5원)까지 안전하게
        ("3000", "5"),
        ("15000", "10"),
        ("30000", "50"),
        ("92647", "100"),
        ("300000", "500"),
        ("700000", "1000"),
    ],
)
def test_tick_bands(price: str, expected: str) -> None:
    assert tick_size(Decimal(price)) == Decimal(expected)


def test_fractional_average_price_is_aligned() -> None:
    """평단가 폴백(소수점)이 그대로 나가면 거부된다 — 정렬돼야 한다."""
    avg = Decimal("88864.409940459151325")
    for side in (Side.BUY, Side.SELL):
        out = round_to_tick(avg, side)
        assert out % tick_size(out) == 0
        assert out.as_tuple().exponent >= 0  # 소수점 없음


def test_rounding_direction_favors_execution() -> None:
    """매수는 올림(비싸게 사서 체결↑), 매도는 내림(싸게 팔아 체결↑).
    손절이 호가 정렬 탓에 미체결로 남는 것보다 한 틱 불리한 게 낫다."""
    price = Decimal("92647")  # tick 100
    assert round_to_tick(price, Side.BUY) == Decimal("92700")
    assert round_to_tick(price, Side.SELL) == Decimal("92600")


def test_already_aligned_price_unchanged() -> None:
    for side in (Side.BUY, Side.SELL):
        assert round_to_tick(Decimal("92600"), side) == Decimal("92600")


def test_never_returns_zero() -> None:
    """0원 주문은 거부된다 — 매도 내림이 0으로 떨어지면 안 된다."""
    assert round_to_tick(Decimal("3"), Side.SELL) == Decimal("5")


def test_etf_price_is_multiple_of_five() -> None:
    """2,000원 이상 구간의 호가단위는 모두 5의 배수라 ETF 에도 유효하다."""
    for p in ("2500", "15000", "30000", "92647", "300000", "700000"):
        for side in (Side.BUY, Side.SELL):
            assert round_to_tick(Decimal(p), side) % 5 == 0
