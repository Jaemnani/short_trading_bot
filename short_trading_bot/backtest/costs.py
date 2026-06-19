"""KR trading-cost model for realistic backtests / paper fills.

- Brokerage fee on both sides (~0.015%).
- 증권거래세 (securities transaction tax) on SELLS only, KRX domestic (~0.18%, declining
  schedule — verify current rate); overseas has no KR transaction tax.
- Slippage applied to the reference (close) price.

NOTE: ±30% price limits, 상한가/하한가 lock-ups, VI pauses, and T+2 cash settlement are
not yet modeled — paper trading (P9) validates those before live.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.enums import Market

_BPS = Decimal(10000)


@dataclass(slots=True)
class CostModel:
    fee_bps: float = 1.5  # ~0.015% brokerage, each side
    sell_tax_bps: float = 18.0  # ~0.18% KRX 증권거래세 (sell only)
    slippage_bps: float = 5.0

    def buy_price(self, ref: Decimal) -> Decimal:
        return ref * (Decimal(1) + Decimal(str(self.slippage_bps)) / _BPS)

    def sell_price(self, ref: Decimal) -> Decimal:
        return ref * (Decimal(1) - Decimal(str(self.slippage_bps)) / _BPS)

    def fee(self, notional: Decimal) -> Decimal:
        return notional * Decimal(str(self.fee_bps)) / _BPS

    def sell_tax(self, notional: Decimal, market: Market) -> Decimal:
        if market is Market.KRX:
            return notional * Decimal(str(self.sell_tax_bps)) / _BPS
        return Decimal(0)
