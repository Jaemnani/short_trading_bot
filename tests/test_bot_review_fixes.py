"""Regression tests for the day-trading-bot review fixes (D1 bars, resolution guard,
fill-delta accounting, hydrated-lot take-profit)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from short_trading_bot.domain.enums import Market, PositionState, Resolution, Side
from short_trading_bot.domain.factory import PositionFactory
from short_trading_bot.domain.signal import Intent, IntentKind, Signal
from short_trading_bot.execution.broker.paper import PaperBrokerAdapter, PaperConfig
from short_trading_bot.execution.fill_poller import FillPoller
from short_trading_bot.execution.types import Execution, Fill
from short_trading_bot.market.bar_builder import BarBuilder
from short_trading_bot.market.types import Tick
from short_trading_bot.persistence.db import session_scope
from short_trading_bot.persistence.models import Fill as FillRow
from short_trading_bot.persistence.models import Order
from short_trading_bot.strategy.templates import StrategyTemplate

from ._helpers import make_snapshot

BASE = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)


# --- Fix 1: BarBuilder aggregates daily (1D) bars ---

def test_bar_builder_daily_aggregation() -> None:
    bb = BarBuilder(Resolution.D1)
    bb.on_tick(Tick("005930", Decimal("100"), Decimal("10"), BASE))
    bb.on_tick(Tick("005930", Decimal("110"), Decimal("5"), BASE + timedelta(hours=3)))
    done = bb.on_tick(Tick("005930", Decimal("105"), Decimal("1"), BASE + timedelta(days=1)))
    assert len(done) == 1
    bar = done[0]
    assert bar.resolution is Resolution.D1
    assert (bar.open, bar.high, bar.close) == (Decimal("100"), Decimal("110"), Decimal("110"))
    assert bar.volume == Decimal("15")


# --- Fix 2: lot never trades on wrong-resolution bars ---

def test_resolution_mismatch_holds() -> None:
    lot = PositionFactory.create(
        Signal(ticker="005930"),
        StrategyTemplate(strategy_id="trend_long_v1", resolution=Resolution.M5),
    )
    out = lot.evaluate(make_snapshot(100, resolution=Resolution.D1), Decimal("10000000"))
    assert out[0].kind is IntentKind.HOLD
    assert out[0].reason == "resolution_mismatch"


# --- Fix 3: FillPoller applies DELTAS of cumulative 체결내역 ---

class _StubExecBroker:
    """BrokerAdapter stub exposing crafted cumulative executions."""

    fill_handler = None

    def __init__(self) -> None:
        self.executions: list[Execution] = []

    @property
    def name(self) -> str:
        return "stub"

    async def submit_order(self, req):  # pragma: no cover
        raise NotImplementedError

    async def cancel_order(self, req, broker_order_no):  # pragma: no cover
        raise NotImplementedError

    async def get_balance(self):  # pragma: no cover
        raise NotImplementedError

    async def get_open_orders(self):
        return []

    async def get_executions(self) -> list[Execution]:
        return list(self.executions)


async def _seed_order(sf, broker_order_no: str = "B1", cid: str = "c1") -> None:
    async with session_scope(sf) as s:
        s.add(
            Order(
                order_id=f"o-{cid}", lot_id="lot1", client_order_id=cid,
                broker_order_no=broker_order_no, side="BUY", qty=Decimal("10"),
                price=Decimal("70000"), state="NEW",
            )
        )


def _exe(exec_id: str, cum_qty: str, broker_no: str = "B1") -> Execution:
    return Execution(
        exec_id=exec_id, broker_order_no=broker_no, ticker="005930", side=Side.BUY,
        qty=Decimal(cum_qty), price=Decimal("70000"),
    )


async def test_fill_poller_applies_cumulative_deltas(sf) -> None:
    await _seed_order(sf)
    broker = _StubExecBroker()
    collected: list[Fill] = []

    async def handler(f: Fill) -> None:
        collected.append(f)
        async with session_scope(sf) as s:  # persist like OrderManager does
            s.add(
                FillRow(
                    fill_id=f"f{len(collected)}", order_id="o-c1", lot_id="lot1",
                    qty=f.qty, price=f.price, currency="KRW",
                )
            )

    poller = FillPoller(broker, sf, handler)
    broker.executions = [_exe("e1", "4")]  # partial: 4 of 10 filled
    assert await poller.poll_once() == 1
    assert collected[0].qty == Decimal("4")

    broker.executions = [_exe("e2", "10")]  # cumulative now 10 -> delta 6, not 10
    assert await poller.poll_once() == 1
    assert collected[1].qty == Decimal("6")

    # Restart: fresh poller (empty _seen) re-reads the same row -> delta 0, no re-apply
    poller2 = FillPoller(broker, sf, handler)
    assert await poller2.poll_once() == 0
    assert len(collected) == 2


async def test_fill_poller_retries_unresolved(sf) -> None:
    broker = _StubExecBroker()
    broker.executions = [_exe("e1", "10")]
    collected: list[Fill] = []

    async def handler(f: Fill) -> None:
        collected.append(f)

    poller = FillPoller(broker, sf, handler)
    assert await poller.poll_once() == 0  # order row not yet persisted -> skipped, NOT seen
    await _seed_order(sf)
    assert await poller.poll_once() == 1  # retried successfully on the next poll
    assert collected[0].qty == Decimal("10")


# --- Fix 4: fraction-based TRIM converts to absolute qty (hydrated lots can TP) ---

async def test_fraction_trim_converts_to_qty(sf) -> None:
    from short_trading_bot.app.service import TradingService
    from short_trading_bot.market.types import Bar
    from short_trading_bot.risk.limits import RiskLimits, RiskSnapshot
    from short_trading_bot.risk.manager import RiskManager

    broker = PaperBrokerAdapter(PaperConfig(enforce_funds=False))
    svc = TradingService(broker, sf, RiskManager(RiskLimits()), {})
    lot = PositionFactory.create(
        Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1")
    )
    lot.state = PositionState.HOLDING
    lot.qty = Decimal("10")
    lot.avg_entry = Decimal("100")
    svc.lots[svc.lot_key("005930", lot.params.resolution)] = lot
    async with session_scope(sf) as s:
        from short_trading_bot.persistence.models import Position

        s.add(
            Position(
                lot_id=lot.lot_id, ticker="005930", state="HOLDING",
                strategy_id="trend_long_v1", qty_filled=Decimal("10"),
                avg_entry_price=Decimal("100"),
            )
        )

    c = Decimal("120")
    bar = Bar("005930", Resolution.D1, BASE, c, c, c, c, Decimal("1000"), c * Decimal("1000"))
    intent = Intent(kind=IntentKind.TRIM, side=Side.SELL, fraction=0.5, reason="take_profit_1")
    await svc._handle_intent(
        intent, lot, bar, RiskSnapshot(equity=Decimal("10000000"))
    )
    assert lot.qty == Decimal("5")  # 0.5 of 10 sold — fraction was converted, not dropped


def test_market_enum_used() -> None:  # keep the Market import meaningful
    assert Market.KRX.value == "KRX"
