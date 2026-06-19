"""Core domain enumerations shared across the engine.

All enums subclass ``str`` so they serialize cleanly to JSON / DB columns and
compare equal to their string values.
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """Execution mode. Switching PAPER<->LIVE only changes broker base URL + TR_ID prefix."""

    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Resolution(StrEnum):
    """Per-position bar resolution. Long-only is a *direction*; resolution sets *frequency*.

    Supports every standard bar from tick to monthly so a lot can scalp on ticks/1m
    or swing on daily/weekly. Minute bars are aggregated from ticks by the BarBuilder
    or loaded natively from KIS; daily/weekly/monthly come from period-bar loaders.
    """

    TICK = "TICK"
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M10 = "10m"
    M15 = "15m"
    M30 = "30m"
    M60 = "60m"
    D1 = "1D"
    W1 = "1W"
    MO1 = "1M"

    @property
    def is_intraday(self) -> bool:
        return self in _INTRADAY

    @property
    def bar_seconds(self) -> int | None:
        """Seconds per bar for minute resolutions; ``None`` for tick & calendar bars."""
        return _BAR_SECONDS.get(self)


_INTRADAY: frozenset[Resolution] = frozenset(
    {
        Resolution.TICK,
        Resolution.M1,
        Resolution.M3,
        Resolution.M5,
        Resolution.M10,
        Resolution.M15,
        Resolution.M30,
        Resolution.M60,
    }
)

_BAR_SECONDS: dict[Resolution, int] = {
    Resolution.M1: 60,
    Resolution.M3: 180,
    Resolution.M5: 300,
    Resolution.M10: 600,
    Resolution.M15: 900,
    Resolution.M30: 1800,
    Resolution.M60: 3600,
}


class Market(StrEnum):
    """Tradable markets. KRX is domestic; the rest are overseas (KIS overseas-stock API).

    The value is the KIS *trading* exchange code (``OVRS_EXCG_CD``); the separate
    price/WS code (``EXCD``) is exposed via :pyattr:`price_code`.
    """

    KRX = "KRX"
    NASD = "NASD"
    NYSE = "NYSE"
    AMEX = "AMEX"
    SEHK = "SEHK"
    TKSE = "TKSE"
    SHAA = "SHAA"
    SZAA = "SZAA"
    HASE = "HASE"
    VNSE = "VNSE"

    @property
    def is_overseas(self) -> bool:
        return self is not Market.KRX

    @property
    def currency(self) -> Currency:
        return _MARKET_CURRENCY[self]

    @property
    def price_code(self) -> str | None:
        """KIS overseas price/WS exchange code (``EXCD``); ``None`` for domestic."""
        return _MARKET_PRICE_CODE.get(self)


class Currency(StrEnum):
    KRW = "KRW"
    USD = "USD"
    HKD = "HKD"
    JPY = "JPY"
    CNY = "CNY"
    VND = "VND"


_MARKET_CURRENCY: dict[Market, Currency] = {
    Market.KRX: Currency.KRW,
    Market.NASD: Currency.USD,
    Market.NYSE: Currency.USD,
    Market.AMEX: Currency.USD,
    Market.SEHK: Currency.HKD,
    Market.TKSE: Currency.JPY,
    Market.SHAA: Currency.CNY,
    Market.SZAA: Currency.CNY,
    Market.HASE: Currency.VND,
    Market.VNSE: Currency.VND,
}

# KIS price/WS exchange codes (EXCD) — distinct from the trading OVRS_EXCG_CD above.
_MARKET_PRICE_CODE: dict[Market, str] = {
    Market.NASD: "NAS",
    Market.NYSE: "NYS",
    Market.AMEX: "AMS",
    Market.SEHK: "HKS",
    Market.TKSE: "TSE",
    Market.SHAA: "SHS",
    Market.SZAA: "SZS",
    Market.HASE: "HNX",
    Market.VNSE: "HSX",
}


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PositionState(StrEnum):
    WATCHING = "WATCHING"
    HOLDING = "HOLDING"
    SCALING = "SCALING"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class OrderState(StrEnum):
    PENDING_NEW = "PENDING_NEW"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class CampaignState(StrEnum):
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    LIQUIDATING = "LIQUIDATING"
    SETTLED = "SETTLED"


class ControlState(StrEnum):
    """Remote control state — settable from any device, persisted atomically."""

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"  # block new entries, keep managing/stopping open lots
    STOPPED = "STOPPED"  # kill switch: halt + flat-all
