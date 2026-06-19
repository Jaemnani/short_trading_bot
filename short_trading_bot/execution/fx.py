"""FxManager — multi-currency accounting for overseas trading.

⚠️ KIS Developers has NO on-demand 환전 (KRW<->foreign) REST API. The bot relies on
**통합증거금 (integrated margin) auto-FX**: overseas orders are funded from KRW deposits and
the broker auto-converts (가환전→정산) on the settlement schedule at a broker-controlled rate.
This manager therefore only READS balances/rates and computes KRW-equivalent values;
:meth:`request_conversion` always reports that explicit conversion must be done manually.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..domain.enums import Currency, Market


@dataclass(slots=True)
class FxRates:
    """Settlement exchange rates to KRW (e.g. {USD: 1350})."""

    to_krw: dict[Currency, Decimal] = field(default_factory=dict)

    def rate(self, currency: Currency) -> Decimal:
        if currency is Currency.KRW:
            return Decimal(1)
        return self.to_krw.get(currency, Decimal(0))


@dataclass(slots=True)
class FxConversionResult:
    executed: bool
    reason: str


class FxManager:
    def __init__(self, *, integrated_margin: bool = True) -> None:
        self._integrated_margin = integrated_margin

    def to_krw(self, amount: Decimal, currency: Currency, rates: FxRates) -> Decimal:
        return amount * rates.rate(currency)

    def total_krw(self, balances: dict[Currency, Decimal], rates: FxRates) -> Decimal:
        return sum((self.to_krw(amt, ccy, rates) for ccy, amt in balances.items()), Decimal(0))

    def krw_pnl(self, local_pnl: Decimal, currency: Currency, rate: Decimal) -> Decimal:
        """Convert a local-currency realized P&L to KRW using the settlement rate."""
        return local_pnl * (Decimal(1) if currency is Currency.KRW else rate)

    def available_for_overseas(
        self, balances: dict[Currency, Decimal], rates: FxRates, market: Market
    ) -> Decimal:
        """KRW-equivalent buying power for an overseas market.

        With 통합증거금, the whole KRW(+foreign) balance funds overseas buys (broker auto-FX).
        Without it, only the market's own foreign-currency deposit is usable.
        """
        if self._integrated_margin:
            return self.total_krw(balances, rates)
        amount = balances.get(market.currency, Decimal(0))
        return self.to_krw(amount, market.currency, rates)

    def request_conversion(
        self, amount: Decimal, from_ccy: Currency, to_ccy: Currency
    ) -> FxConversionResult:
        """On-demand 환전 is NOT available via the KIS API — always reports manual-required."""
        return FxConversionResult(
            executed=False,
            reason=(
                "KIS API has no on-demand 환전 endpoint; use 통합증거금 auto-FX for KRW-funded "
                "overseas buying, or convert manually in HTS/MTS."
            ),
        )
