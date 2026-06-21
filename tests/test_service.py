from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from short_trading_bot.app.service import TradingService
from short_trading_bot.domain.enums import PositionState, Resolution
from short_trading_bot.domain.params import PositionParams
from short_trading_bot.execution.broker.paper import PaperBrokerAdapter, PaperConfig
from short_trading_bot.infra.notifier.base import InMemoryNotifier
from short_trading_bot.market.feed import ReplayFeed
from short_trading_bot.market.types import Bar
from short_trading_bot.persistence.db import session_scope
from short_trading_bot.persistence.models import Order, Position
from short_trading_bot.risk.limits import RiskLimits
from short_trading_bot.risk.manager import RiskManager
from short_trading_bot.strategy.templates import StrategyTemplate

TICKER = "005930"


def _bar(close: float, ts: datetime) -> Bar:
    c = Decimal(str(close))
    return Bar(
        ticker=TICKER, resolution=Resolution.D1, ts=ts,
        open=c, high=c, low=c - 1, close=c, volume=Decimal("1000"), value=c * Decimal("1000"),
    )


def _uptrend(n: int = 80) -> list[Bar]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return [_bar(100.0 + 2 * i, base + timedelta(days=i)) for i in range(n)]


def _watchlist() -> dict[str, StrategyTemplate]:
    return {
        TICKER: StrategyTemplate(
            strategy_id="trend_long_v1",
            resolution=Resolution.D1,
            strategy_params={"require_confirm": False},
        )
    }


def _service(sf: async_sessionmaker[AsyncSession], **kw) -> tuple[TradingService, PaperBrokerAdapter, InMemoryNotifier]:
    broker = PaperBrokerAdapter(PaperConfig(initial_cash=Decimal("100000000")))
    notifier = InMemoryNotifier()
    risk = RiskManager(RiskLimits(), **kw)
    svc = TradingService(broker, sf, risk, _watchlist(), notifier=notifier)
    return svc, broker, notifier


async def _order_count(sf: async_sessionmaker[AsyncSession]) -> int:
    async with session_scope(sf) as s:
        return (await s.execute(select(func.count()).select_from(Order))).scalar_one()


async def test_service_enters_position(sf) -> None:
    svc, broker, notifier = _service(sf)
    await svc.run(ReplayFeed(_uptrend()))

    lot = svc.lots[TICKER]
    assert lot.is_open and lot.qty > 0  # entered and holding
    assert await _order_count(sf) > 0  # OrderManager persisted orders
    bal = await broker.get_balance()
    assert any(p.ticker == TICKER for p in bal.positions)  # broker holds shares
    assert any(n.event == "order.accepted" for n in notifier.sent)


async def test_service_pause_blocks_entry(sf) -> None:
    from short_trading_bot.risk.control import ControlSwitch

    control = ControlSwitch()
    control.pause()
    svc, broker, notifier = _service(sf, control=control)
    await svc.run(ReplayFeed(_uptrend()))

    assert svc.lots[TICKER].state is PositionState.WATCHING  # never entered
    bal = await broker.get_balance()
    assert bal.positions == []
    assert any(n.event == "intent.blocked" and n.fields.get("reason") == "paused" for n in notifier.sent)


async def test_service_hydrate_from_db(sf) -> None:
    params = PositionParams(strategy_id="trend_long_v1")
    async with session_scope(sf) as s:
        s.add(
            Position(
                lot_id="lotX", ticker=TICKER, market="KRX", currency="KRW", side="BUY",
                state="HOLDING", strategy_id="trend_long_v1",
                params_json=params.model_dump(mode="json"), resolution="1D",
                qty_filled=Decimal("10"), avg_entry_price=Decimal("70000"),
            )
        )
    svc, _broker, _ = _service(sf)
    restored = await svc.hydrate()
    assert restored == 1
    lot = svc.lots[TICKER]
    assert lot.state is PositionState.HOLDING
    assert lot.qty == Decimal("10") and lot.avg_entry == Decimal("70000")


async def test_service_kill_switch_flattens(sf) -> None:
    svc, broker, _ = _service(sf)
    tape = _uptrend()
    await svc.run(ReplayFeed(tape))
    assert svc.lots[TICKER].is_open  # holding before kill switch

    svc.control.stop()  # 긴급중지
    extra = _bar(260.0, tape[-1].ts + timedelta(days=1))
    await svc.process(extra)  # flat-all runs at start of process

    assert svc.lots[TICKER].state is PositionState.CLOSED
    bal = await broker.get_balance()
    assert all(p.qty == 0 for p in bal.positions)
