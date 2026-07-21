"""Estimated commission/tax for adapters whose 체결내역 API carries no cost fields.

KIS 일별주문체결조회(inquire-daily-ccld)·해외 체결내역 응답에는 수수료·제세금 필드가
없다. 정산 조회 API를 붙이기 전까지 요율 기반 추정으로 채운다 — 0으로 두면 실현손익이
낙관적으로 계산되어 일일 손실 한도 브레이크가 실제보다 늦게 걸린다. 추정은 보수적
(ETF 매도에도 거래세율 적용 → 비용 과대평가)이며 페이퍼 브로커 기본 요율과 일치시켜
페이퍼/라이브 손익이 같은 기준으로 비교되게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.enums import Side

_BPS = Decimal(10000)


@dataclass(frozen=True, slots=True)
class FeeModel:
    """Rate-based cost estimate in bps of executed notional."""

    fee_bps: Decimal = Decimal("1.77")  # KIS 비대면 온라인 0.0140527% + 유관기관제비용 0.0036396%
    sell_tax_bps: Decimal = Decimal("20")  # 증권거래세+농특세 0.20% (2026 KRX, 매도만)

    def fee(self, notional: Decimal) -> Decimal:
        return notional * self.fee_bps / _BPS

    def tax(self, side: Side, notional: Decimal) -> Decimal:
        return notional * self.sell_tax_bps / _BPS if side is Side.SELL else Decimal(0)


KRX_FEES = FeeModel()
# 미국주식 온라인 수수료 0.25%; 매도 SEC fee는 무시 가능한 수준이라 0 처리.
OVERSEAS_FEES = FeeModel(fee_bps=Decimal("25"), sell_tax_bps=Decimal("0"))
