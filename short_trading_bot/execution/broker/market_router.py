"""Resolve KIS TR_IDs per (market, side, mode).

Paper(모의) TR_IDs = live with the leading char replaced by 'V' (TTTC->VTTC, TTTT->VTTT,
TTTS->VTTS). Trading uses OVRS_EXCG_CD (Market.value: NASD/NYSE/...); price/WS use the
distinct EXCD (Market.price_code: NAS/NYS/...), exposed on the Market enum.

⚠️ Some overseas TR_IDs (esp. amend/cancel) are UNVERIFIED — confirm on the KIS portal /
official sample repo before live use.
"""

from __future__ import annotations

from ...domain.enums import Market, Mode, Side

_US = (Market.NASD, Market.NYSE, Market.AMEX)

# Live overseas order TR_IDs (buy, sell) per market.
_OVERSEAS_ORDER: dict[Market, tuple[str, str]] = {
    Market.NASD: ("TTTT1002U", "TTTT1006U"),
    Market.NYSE: ("TTTT1002U", "TTTT1006U"),
    Market.AMEX: ("TTTT1002U", "TTTT1006U"),
    Market.SEHK: ("TTTS1002U", "TTTS1001U"),
    Market.SHAA: ("TTTS0202U", "TTTS1005U"),
    Market.SZAA: ("TTTS0305U", "TTTS0304U"),
    Market.TKSE: ("TTTS0308U", "TTTS0307U"),
    Market.HASE: ("TTTS0311U", "TTTS0310U"),
    Market.VNSE: ("TTTS0311U", "TTTS0310U"),
}

# Domestic (KRX) order TR_IDs (live).
_DOMESTIC_ORDER = {Side.BUY: "TTTC0802U", Side.SELL: "TTTC0801U"}

# Balance inquiry TR_IDs (live): domestic vs overseas.
_DOMESTIC_BALANCE = "TTTC8434R"
_OVERSEAS_BALANCE = "TTTS3012R"


def _paper(tr_id: str) -> str:
    """Live -> paper: replace the leading char with 'V' (TTTC0802U -> VTTC0802U)."""
    return "V" + tr_id[1:]


class MarketRouter:
    """Stateless resolver; inject a custom subclass to override TR_IDs if KIS changes them."""

    def order_tr_id(self, market: Market, side: Side, mode: Mode) -> str:
        if market is Market.KRX:
            tr_id = _DOMESTIC_ORDER[side]
        else:
            pair = _OVERSEAS_ORDER.get(market)
            if pair is None:
                raise ValueError(f"no order TR_ID for market {market.value}")
            tr_id = pair[0] if side is Side.BUY else pair[1]
        return _paper(tr_id) if mode is Mode.PAPER else tr_id

    def balance_tr_id(self, market: Market, mode: Mode) -> str:
        tr_id = _DOMESTIC_BALANCE if market is Market.KRX else _OVERSEAS_BALANCE
        return _paper(tr_id) if mode is Mode.PAPER else tr_id

    @staticmethod
    def trading_exchange_code(market: Market) -> str:
        """OVRS_EXCG_CD used in trading bodies (NASD/NYSE/SEHK/...)."""
        return market.value

    @staticmethod
    def price_exchange_code(market: Market) -> str | None:
        """EXCD used in price/WS lookups (NAS/NYS/HKS/...); None for domestic."""
        return market.price_code
