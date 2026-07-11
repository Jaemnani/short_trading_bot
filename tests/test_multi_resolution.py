"""멀티 해상도 동시 운용 tests — 같은 종목을 1D와 60m에서 독립적으로 (한 프로세스/한 WS)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from short_trading_bot.app.service import TradingService
from short_trading_bot.domain.enums import PositionState, Resolution
from short_trading_bot.execution.broker.paper import PaperBrokerAdapter, PaperConfig
from short_trading_bot.market.feed import ReplayFeed
from short_trading_bot.market.types import Bar
from short_trading_bot.risk.limits import RiskLimits
from short_trading_bot.risk.manager import RiskManager
from short_trading_bot.strategy.templates import StrategyTemplate

TICKER = "005930"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(close: float, ts: datetime, res: Resolution) -> Bar:
    c = Decimal(str(close))
    return Bar(ticker=TICKER, resolution=res, ts=ts,
               open=c, high=c, low=c - 1, close=c, volume=Decimal("1000"), value=c * Decimal("1000"))


def _watchlist() -> dict[str, StrategyTemplate]:
    return {
        f"{TICKER}@1D": StrategyTemplate(
            strategy_id="trend_long_v1", resolution=Resolution.D1,
            strategy_params={"require_confirm": False},
        ),
        f"{TICKER}@60m": StrategyTemplate(
            strategy_id="trend_long_v1", resolution=Resolution.M60,
            strategy_params={"require_confirm": False},
        ),
    }


def _service(sf) -> tuple[TradingService, PaperBrokerAdapter]:
    broker = PaperBrokerAdapter(PaperConfig(initial_cash=Decimal("100000000")))
    svc = TradingService(broker, sf, RiskManager(RiskLimits()), _watchlist())
    return svc, broker


async def test_same_ticker_two_resolutions_run_independently(sf) -> None:
    svc, _broker = _service(sf)
    # 일봉 상승 추세 80개 → 1D lot만 진입해야 함 (60m lot은 봉이 없으니 미생성)
    daily = [_bar(100.0 + 2 * i, BASE + timedelta(days=i), Resolution.D1) for i in range(80)]
    await svc.run(ReplayFeed(daily))

    d1 = svc.lot(TICKER, Resolution.D1)
    assert d1 is not None and d1.is_open
    assert svc.lot(TICKER, Resolution.M60) is None  # 60m 봉을 아직 못 받음

    # 60m 봉이 흐르기 시작 → 별도 lot이 WATCHING으로 생성, 1D lot은 그대로
    hourly = _bar(260.0, BASE + timedelta(days=80, hours=1), Resolution.M60)
    await svc.process(hourly)
    m60 = svc.lot(TICKER, Resolution.M60)
    assert m60 is not None and m60.state is PositionState.WATCHING
    assert m60 is not d1 and svc.lot(TICKER, Resolution.D1).is_open  # 서로 독립

    # lot 키가 종목@해상도로 분리되어 있다
    assert set(svc.lots) >= {f"{TICKER}@1D", f"{TICKER}@60m"}


async def test_duplicate_ticker_resolution_rejected(sf) -> None:
    broker = PaperBrokerAdapter(PaperConfig())
    dup = {
        f"{TICKER}@a": StrategyTemplate(strategy_id="trend_long_v1", resolution=Resolution.D1),
        f"{TICKER}@b": StrategyTemplate(strategy_id="trend_long_v1", resolution=Resolution.D1),
    }
    with pytest.raises(ValueError):
        TradingService(broker, sf, RiskManager(RiskLimits()), dup)


async def test_hydrate_keys_by_resolution(sf) -> None:
    """복원 시 params의 해상도로 키잉 — 다른 해상도 lot과 충돌하지 않는다."""
    from short_trading_bot.domain.params import PositionParams
    from short_trading_bot.persistence.db import session_scope
    from short_trading_bot.persistence.models import Position

    params_1d = PositionParams(strategy_id="trend_long_v1", resolution=Resolution.D1)
    async with session_scope(sf) as s:
        s.add(Position(
            lot_id="lot1d", ticker=TICKER, market="KRX", currency="KRW", side="BUY",
            state="HOLDING", strategy_id="trend_long_v1",
            params_json=params_1d.model_dump(mode="json"), resolution="1D",
            qty_filled=Decimal("10"), avg_entry_price=Decimal("70000"),
        ))
    svc, _ = _service(sf)
    assert await svc.hydrate() == 1
    assert svc.lot(TICKER, Resolution.D1).qty == Decimal("10")
    assert svc.lot(TICKER, Resolution.M60) is None  # 60m 자리는 비어 있음