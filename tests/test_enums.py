from short_trading_bot.domain.enums import Currency, Market, Resolution


def test_resolution_bar_seconds() -> None:
    assert Resolution.M1.bar_seconds == 60
    assert Resolution.M5.bar_seconds == 300
    assert Resolution.M60.bar_seconds == 3600
    assert Resolution.TICK.bar_seconds is None
    assert Resolution.D1.bar_seconds is None  # calendar bar


def test_resolution_intraday() -> None:
    assert Resolution.TICK.is_intraday
    assert Resolution.M30.is_intraday
    assert not Resolution.D1.is_intraday
    assert not Resolution.W1.is_intraday


def test_market_overseas_and_currency() -> None:
    assert Market.KRX.is_overseas is False
    assert Market.KRX.currency is Currency.KRW
    assert Market.KRX.price_code is None

    assert Market.NASD.is_overseas is True
    assert Market.NASD.currency is Currency.USD
    assert Market.NASD.price_code == "NAS"  # EXCD distinct from OVRS_EXCG_CD 'NASD'
    assert Market.SEHK.currency is Currency.HKD
    assert Market.TKSE.price_code == "TSE"
