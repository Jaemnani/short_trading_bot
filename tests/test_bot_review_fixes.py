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
from short_trading_bot.execution.types import AccountBalance, Execution, Fill, OrderAck
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


def _exe(
    exec_id: str,
    cum_qty: str,
    broker_no: str = "B1",
    *,
    price: str = "70000",
    fee: str = "0",
) -> Execution:
    return Execution(
        exec_id=exec_id, broker_order_no=broker_no, ticker="005930", side=Side.BUY,
        qty=Decimal(cum_qty), price=Decimal(price), fee=Decimal(fee),
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


async def test_fill_poller_derives_price_and_fee_deltas(sf) -> None:
    await _seed_order(sf)
    broker = _StubExecBroker()
    collected: list[Fill] = []

    async def handler(f: Fill) -> None:
        collected.append(f)
        async with session_scope(sf) as s:
            s.add(FillRow(
                fill_id=f"delta-{len(collected)}", order_id="o-c1", lot_id="lot1",
                qty=f.qty, price=f.price, fee=f.fee, tax=f.tax, currency="KRW",
            ))

    poller = FillPoller(broker, sf, handler)
    broker.executions = [_exe("e1", "4", price="100", fee="4")]
    await poller.poll_once()
    # Cumulative average is now 106 over 10 shares: total 1060. Previous total was 400,
    # therefore the new six shares filled at 110 and incurred only the fee delta 6.
    broker.executions = [_exe("e2", "10", price="106", fee="10")]
    await poller.poll_once()
    assert collected[1].qty == Decimal("6")
    assert collected[1].price == Decimal("110")
    assert collected[1].fee == Decimal("6")


class _WorkingBroker(_StubExecBroker):
    def __init__(self) -> None:
        super().__init__()
        self.requests = []

    async def submit_order(self, req):
        self.requests.append(req)
        return OrderAck(req.client_order_id, True, broker_order_no=f"B{len(self.requests)}")

    async def get_balance(self):
        return AccountBalance()


async def test_service_does_not_duplicate_working_order(sf) -> None:
    from short_trading_bot.app.service import TradingService
    from short_trading_bot.persistence.models import Position
    from short_trading_bot.risk.limits import RiskLimits
    from short_trading_bot.risk.manager import RiskManager

    broker = _WorkingBroker()
    svc = TradingService(broker, sf, RiskManager(RiskLimits()), {})
    lot = PositionFactory.create(Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1"))
    async with session_scope(sf) as s:
        s.add(Position(lot_id=lot.lot_id, ticker=lot.ticker, state="WATCHING", strategy_id="trend_long_v1"))

    await svc._submit(lot, Side.BUY, Decimal("10"), Decimal("100"), is_add=False, reason="entry")
    await svc._submit(lot, Side.BUY, Decimal("10"), Decimal("101"), is_add=False, reason="entry_again")
    assert len(broker.requests) == 1
    assert len(broker.requests[0].client_order_id.split("-")[-1]) == 32


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


# --- Adversarial-review fixes: pending-lock release, FX policy, TP rung refund, ---
# --- notional-regression guard, position-state projection                       ---


def _make_service(sf, broker, **kwargs):
    from short_trading_bot.app.service import TradingService
    from short_trading_bot.risk.limits import RiskLimits
    from short_trading_bot.risk.manager import RiskManager

    return TradingService(broker, sf, RiskManager(RiskLimits()), {}, **kwargs)


async def test_pending_lock_released_when_order_terminal(sf) -> None:
    """A cancelled/expired order must not block the (lot, side) forever."""
    from sqlalchemy import select

    from short_trading_bot.persistence.models import Position

    broker = _WorkingBroker()
    svc = _make_service(sf, broker)
    lot = PositionFactory.create(Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1"))
    async with session_scope(sf) as s:
        s.add(Position(lot_id=lot.lot_id, ticker=lot.ticker, state="WATCHING", strategy_id="trend_long_v1"))

    assert await svc._submit(lot, Side.SELL, Decimal("10"), Decimal("100"), is_add=False, reason="exit")
    # Broker cancels the remainder (e.g. day-order expiry) — no fill event fires.
    async with session_scope(sf) as s:
        order = (await s.execute(select(Order).where(Order.lot_id == lot.lot_id))).scalar_one()
        order.state = "CANCELLED"
    # The lock must be released so the stop-loss / kill-switch sell can go out.
    assert await svc._submit(lot, Side.SELL, Decimal("10"), Decimal("99"), is_add=False, reason="stop_loss")
    assert len(broker.requests) == 2


async def test_hydrate_restores_pending_locks(sf) -> None:
    """Restart must not re-order while a pre-restart order may still be live."""
    from short_trading_bot.persistence.models import Position

    template = StrategyTemplate(strategy_id="trend_long_v1")
    lot = PositionFactory.create(Signal(ticker="005930"), template)
    async with session_scope(sf) as s:
        s.add(Position(
            lot_id=lot.lot_id, ticker="005930", state="HOLDING", strategy_id="trend_long_v1",
            params_json=lot.params.model_dump(mode="json"), qty_filled=Decimal("10"),
            avg_entry_price=Decimal("100"),
        ))
        s.add(Order(
            order_id="o-live", lot_id=lot.lot_id, client_order_id="pre-restart-cid",
            side="BUY", qty=Decimal("5"), price=Decimal("100"), state="NEW",
        ))

    broker = _WorkingBroker()
    svc = _make_service(sf, broker)
    assert await svc.hydrate() == 1
    hydrated = svc.lot("005930")
    assert not await svc._submit(hydrated, Side.BUY, Decimal("5"), Decimal("100"), is_add=True, reason="add")
    assert broker.requests == []


async def test_equity_missing_fx_rate_degrades_instead_of_raising(sf) -> None:
    """USD dust in the balance must not crash the bar loop; it is valued at 0."""
    from short_trading_bot.domain.enums import Currency

    class _DustBroker(_WorkingBroker):
        async def get_balance(self):
            return AccountBalance(cash={Currency.KRW: Decimal("1000000"), Currency.USD: Decimal("0.53")})

    svc = _make_service(sf, _DustBroker())
    assert await svc._equity() == Decimal("1000000")


async def test_exit_intent_not_blocked_by_missing_fx_rate(sf) -> None:
    """Risk-reducing EXIT must go out even when the FX rate is missing; entries stay blocked."""
    from short_trading_bot.domain.enums import Currency, Resolution
    from short_trading_bot.infra.notifier.base import InMemoryNotifier
    from short_trading_bot.market.types import Bar
    from short_trading_bot.persistence.models import Position
    from short_trading_bot.risk.limits import RiskSnapshot

    notifier = InMemoryNotifier()
    broker = _WorkingBroker()
    svc = _make_service(sf, broker, notifier=notifier)
    template = StrategyTemplate(strategy_id="trend_long_v1", market=Market.NASD)
    lot = PositionFactory.create(Signal(ticker="AAPL", market=Market.NASD), template)
    assert lot.currency is Currency.USD
    lot.state = PositionState.HOLDING
    lot.qty = Decimal("10")
    lot.avg_entry = Decimal("100")
    async with session_scope(sf) as s:
        s.add(Position(lot_id=lot.lot_id, ticker="AAPL", state="HOLDING", strategy_id="trend_long_v1"))

    c = Decimal("120")
    bar = Bar("AAPL", Resolution.D1, BASE, c, c, c, c, Decimal("1000"), c * Decimal("1000"))
    snap = RiskSnapshot(equity=Decimal("10000000"))
    await svc._handle_intent(
        Intent(kind=IntentKind.EXIT, side=Side.SELL, reason="stop_loss"), lot, bar, snap
    )
    assert len(broker.requests) == 1  # exit went out despite rate(USD) == 0

    await svc._handle_intent(
        Intent(kind=IntentKind.ADD, side=Side.BUY, qty=Decimal("5"), reason="pyramid"), lot, bar, snap
    )
    assert len(broker.requests) == 1  # entry stayed blocked
    assert any(n.fields.get("reason") == "missing_fx_rate" for n in notifier.sent)


async def test_tp_rung_refunded_when_submit_skipped(sf) -> None:
    """An emit-time-consumed TP rung is given back if the trim order never went out."""
    from short_trading_bot.domain.enums import Resolution
    from short_trading_bot.market.types import Bar
    from short_trading_bot.persistence.models import Position
    from short_trading_bot.risk.limits import RiskSnapshot

    broker = _WorkingBroker()
    svc = _make_service(sf, broker)
    lot = PositionFactory.create(Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1"))
    lot.state = PositionState.HOLDING
    lot.qty = Decimal("10")
    lot.avg_entry = Decimal("100")
    lot.tp_rungs_taken = 1  # advanced at emit time by PositionLot.evaluate
    async with session_scope(sf) as s:
        s.add(Position(lot_id=lot.lot_id, ticker="005930", state="HOLDING", strategy_id="trend_long_v1"))
        s.add(Order(  # a SELL for this lot is still working -> the trim will be skipped
            order_id="o-working", lot_id=lot.lot_id, client_order_id="working-cid",
            side="SELL", qty=Decimal("2"), price=Decimal("100"), state="NEW",
        ))
    svc._pending[(lot.lot_id, Side.SELL)] = "working-cid"

    c = Decimal("120")
    bar = Bar("005930", Resolution.D1, BASE, c, c, c, c, Decimal("1000"), c * Decimal("1000"))
    intent = Intent(kind=IntentKind.TRIM, side=Side.SELL, fraction=0.5, reason="take_profit_1")
    await svc._handle_intent(intent, lot, bar, RiskSnapshot(equity=Decimal("10000000")))
    assert broker.requests == []  # skipped: another SELL is in flight
    assert lot.tp_rungs_taken == 0  # rung refunded so the ladder can re-fire


async def test_fill_poller_notional_regression_falls_back_to_avg_price(sf) -> None:
    """Broker-rounded cumulative averages must never yield a zero/negative fill price."""
    await _seed_order(sf)
    broker = _StubExecBroker()
    collected: list[Fill] = []

    async def handler(f: Fill) -> None:
        collected.append(f)
        async with session_scope(sf) as s:
            s.add(FillRow(
                fill_id=f"nr-{len(collected)}", order_id="o-c1", lot_id="lot1",
                qty=f.qty, price=f.price, fee=f.fee, tax=f.tax, currency="KRW",
            ))

    poller = FillPoller(broker, sf, handler)
    broker.executions = [_exe("e1", "4", price="100")]
    await poller.poll_once()
    # Rounded-down cumulative average: 5 * 79 = 395 < 400 already recorded.
    broker.executions = [_exe("e2", "5", price="79")]
    await poller.poll_once()
    assert collected[1].qty == Decimal("1")
    assert collected[1].price == Decimal("79")  # fell back to the reported average, not -5


async def test_buy_fill_preserves_scaling_state(sf) -> None:
    """The DB projection must not stomp domain-owned states back to HOLDING."""
    from sqlalchemy import select

    from short_trading_bot.execution.order_manager import OrderManager
    from short_trading_bot.persistence.models import Position

    broker = _StubExecBroker()
    om = OrderManager(broker, sf)
    async with session_scope(sf) as s:
        s.add(Position(
            lot_id="lot-sc", ticker="005930", state="SCALING", strategy_id="trend_long_v1",
            qty_filled=Decimal("5"), avg_entry_price=Decimal("100"),
        ))
        s.add(Order(
            order_id="o-sc", lot_id="lot-sc", client_order_id="c-sc",
            side="BUY", qty=Decimal("5"), price=Decimal("110"), state="NEW",
        ))
    await om.handle_fill(Fill(client_order_id="c-sc", qty=Decimal("5"), price=Decimal("110")))
    async with session_scope(sf) as s:
        pos = (await s.execute(select(Position).where(Position.lot_id == "lot-sc"))).scalar_one()
    assert pos.qty_filled == Decimal("10")
    assert pos.state == "SCALING"  # not clobbered to HOLDING
