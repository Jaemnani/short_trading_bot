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

    lot = svc.lot(TICKER)
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

    assert svc.lot(TICKER).state is PositionState.WATCHING  # never entered
    bal = await broker.get_balance()
    assert bal.positions == []
    assert any(n.event == "intent.blocked" and n.fields.get("reason") == "paused" for n in notifier.sent)


async def test_entry_qty_capped_to_max_order_notional(sf) -> None:
    """사이징이 한도를 넘으면 관망(블록)이 아니라 한도에 맞춰 축소 진입해야 한다."""
    cap = Decimal("500000")
    broker = PaperBrokerAdapter(PaperConfig(initial_cash=Decimal("100000000")))
    notifier = InMemoryNotifier()
    svc = TradingService(
        broker, sf, RiskManager(RiskLimits(max_order_notional=cap)), _watchlist(),
        notifier=notifier,
    )
    await svc.run(ReplayFeed(_uptrend()))

    lot = svc.lot(TICKER)
    assert lot.is_open and lot.qty > 0  # 캡 적용으로 진입 자체는 성사
    assert any(n.event == "intent.qty_capped" for n in notifier.sent)
    assert not any(
        n.event == "intent.blocked" and n.fields.get("reason") == "order_notional"
        for n in notifier.sent
    )
    async with session_scope(sf) as s:
        buys = (await s.execute(select(Order).where(Order.side == "BUY"))).scalars().all()
    assert buys and all(o.qty * o.price <= cap for o in buys)
    assert all(o.qty == o.qty.to_integral_value() for o in buys)  # KRX 정수 수량


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
    lot = svc.lot(TICKER)
    assert lot.state is PositionState.HOLDING
    assert lot.qty == Decimal("10") and lot.avg_entry == Decimal("70000")


async def test_runtime_stop_state_survives_restart(sf) -> None:
    """TP 사다리·초기 손절·피크가 DB에 영속되고 재시작(hydrate) 시 복원되어야 한다."""
    svc, _broker, _ = _service(sf)
    await svc.run(ReplayFeed(_uptrend()))
    lot = svc.lot(TICKER)
    assert lot.is_open and lot.tp_rungs_taken >= 1  # 상승장이라 TP1은 밟았을 것

    svc2, _broker2, _ = _service(sf)
    assert await svc2.hydrate() == 1
    restored = svc2.lot(TICKER)
    assert restored.tp_rungs_taken == lot.tp_rungs_taken
    assert restored.initial_stop == lot.initial_stop
    assert restored.peak_price == lot.peak_price
    assert restored.original_qty == lot.original_qty


async def test_daily_realized_resets_on_new_day(sf) -> None:
    svc, _broker, _ = _service(sf)
    day1 = _bar(100.0, datetime(2026, 1, 5, 10, 0, tzinfo=UTC))
    await svc.process(day1)
    svc._daily_realized = Decimal("-300000")  # 당일 실현손실 가정
    assert svc._risk_snapshot(Decimal("10000000")).daily_pnl == Decimal("-300000")

    day2 = _bar(101.0, datetime(2026, 1, 6, 9, 0, tzinfo=UTC))  # 다음 거래일
    await svc.process(day2)
    assert svc._daily_realized == Decimal("0")  # 자정 리셋

    # 피크 자본은 위로만 래칫(자본이 줄어도 피크 유지) — 총 낙폭 브레이크 기준
    peak_before = svc._peak_equity
    higher = peak_before + Decimal("1000000")
    assert svc._risk_snapshot(higher).peak_equity == higher
    assert svc._risk_snapshot(Decimal("1000")).peak_equity == higher  # 하락해도 피크 불변


async def test_service_kill_switch_flattens(sf) -> None:
    svc, broker, _ = _service(sf)
    tape = _uptrend()
    await svc.run(ReplayFeed(tape))
    assert svc.lot(TICKER).is_open  # holding before kill switch

    svc.control.stop()  # 긴급중지
    extra = _bar(260.0, tape[-1].ts + timedelta(days=1))
    await svc.process(extra)  # flat-all runs at start of process

    assert svc.lot(TICKER).state is PositionState.CLOSED
    bal = await broker.get_balance()
    assert all(p.qty == 0 for p in bal.positions)


async def test_regime_filter_blocks_gated_entry(sf) -> None:
    """레짐 나쁨 + regime_filter=True 템플릿 → 신규 진입 차단 (market_regime)."""
    from short_trading_bot.market.regime import MarketRegime

    broker = PaperBrokerAdapter(PaperConfig(initial_cash=Decimal("100000000")))
    notifier = InMemoryNotifier()
    watchlist = _watchlist()
    watchlist[TICKER] = watchlist[TICKER].model_copy(update={"regime_filter": True})
    regime = MarketRegime()
    regime.set_daily(False)  # 전일 코스피 20일선 아래
    svc = TradingService(
        broker, sf, RiskManager(RiskLimits()), watchlist, notifier=notifier, regime=regime
    )
    await svc.run(ReplayFeed(_uptrend()))

    assert svc.lot(TICKER).state is PositionState.WATCHING  # 진입 없음
    assert any(
        n.event == "intent.blocked" and n.fields.get("reason") == "market_regime"
        for n in notifier.sent
    )

    # regime_filter=False(기본값)면 같은 조건에서도 정상 진입 — 기존 동작 불변.
    svc2, _broker2, _ = _service(sf)
    svc2._regime = regime  # 필터 미지정 템플릿은 게이트 대상 아님
    await svc2.run(ReplayFeed(_uptrend()))
    assert svc2.lot(TICKER).is_open
