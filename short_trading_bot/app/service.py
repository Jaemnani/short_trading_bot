"""TradingService — the async engine orchestrator.

Wires the pipeline: Feed → IndicatorEngine → per-ticker PositionLot.evaluate → RiskManager
gate → OrderManager (idempotent) → BrokerAdapter. Fills flow back through a composed handler
that persists (OrderManager) AND syncs the in-memory lot (strategy source of truth). Remote
control: PAUSE blocks new entries (risk gate); STOP requests a flat-all liquidation that the
loop executes promptly. The same service runs over a ReplayFeed (paper-over-history / tests)
or a live KIS WebSocket feed.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..domain.enums import Currency, Market, PositionState, Side
from ..domain.factory import PositionFactory
from ..domain.params import PositionParams
from ..domain.position import PositionLot
from ..domain.signal import Intent, IntentKind, Signal
from ..execution.broker.base import BrokerAdapter
from ..execution.fill_poller import FillPoller
from ..execution.order_manager import OrderManager
from ..execution.reconciler import Reconciler, ReconcileReport
from ..execution.types import Fill, OrderRequest
from ..infra.logging import get_logger
from ..infra.notifier.base import InMemoryNotifier, Notifier
from ..market.feed import Feed
from ..market.indicators import IndicatorEngine
from ..market.types import Bar, IndicatorSnapshot
from ..persistence.db import session_scope
from ..persistence.models import Position
from ..risk.limits import RiskSnapshot
from ..risk.manager import RiskManager
from ..strategy.registry import create_strategy
from ..strategy.templates import StrategyTemplate

_ENTRY_KINDS = (IntentKind.ENTER, IntentKind.ADD)


class TradingService:
    def __init__(
        self,
        broker: BrokerAdapter,
        session_factory: async_sessionmaker[AsyncSession],
        risk: RiskManager,
        watchlist: dict[str, StrategyTemplate],
        *,
        notifier: Notifier | None = None,
        news_ewma: float | None = None,
        news_provider: Callable[[str], float | None] | None = None,
    ) -> None:
        self._broker = broker
        self._sf = session_factory
        self._risk = risk
        self._watchlist = watchlist
        self._om = OrderManager(broker, session_factory)
        self._engine = IndicatorEngine()
        self._notifier = notifier or InMemoryNotifier()
        self._news = news_ewma
        self._news_provider = news_provider  # per-ticker EWMA (overrides scalar news_ewma)
        self._log = get_logger("service")

        self._lots: dict[str, PositionLot] = {}
        self._prev: dict[str, IndicatorSnapshot] = {}
        self._last_price: dict[str, Decimal] = {}
        self._co_map: dict[str, tuple[PositionLot, Side, bool]] = {}
        self._counter = 0
        self._daily_realized = Decimal(0)  # 당일 실현손익 (수수료·세금 포함), 날짜 바뀌면 리셋
        self._daily_date: object | None = None
        self._peak_equity = Decimal(0)  # high-water mark (총 낙폭 브레이크 기준)

        # Override OrderManager's fill handler with a composed one (persist + lot sync).
        broker.fill_handler = self._on_fill
        self.control = risk.control

    @property
    def lots(self) -> dict[str, PositionLot]:
        return self._lots

    async def hydrate(self) -> int:
        """Rebuild in-memory lots from the DB Position projection (restart recovery).

        Restores qty/avg/realized/state. Runtime stop state (initial_stop, peak_price,
        tp_rungs_taken) is not yet persisted on the projection, so a hydrated lot manages
        on indicator/trailing exits until those columns are added; reconcile against the
        broker before resuming trading.
        """
        open_states = [
            PositionState.HOLDING.value,
            PositionState.SCALING.value,
            PositionState.EXITING.value,
            PositionState.WATCHING.value,
        ]
        async with session_scope(self._sf) as session:
            rows = (
                await session.execute(select(Position).where(Position.state.in_(open_states)))
            ).scalars().all()
        restored = 0
        for row in rows:
            if row.ticker in self._lots:
                continue
            params = PositionParams(**row.params_json)
            self._lots[row.ticker] = PositionLot(
                lot_id=row.lot_id,
                ticker=row.ticker,
                market=Market(row.market),
                currency=Currency(row.currency),
                params=params,
                strategy=create_strategy(params.strategy_id, params.strategy_params),
                state=PositionState(row.state),
                qty=row.qty_filled,
                avg_entry=row.avg_entry_price,
                realized_pnl=row.realized_pnl,
                peak_price=row.avg_entry_price,
                original_qty=row.qty_filled,  # best effort: TP fractions base on current qty
            )
            restored += 1
        return restored

    def prime(self, bars: list[Bar]) -> int:
        """과거 봉으로 지표 워밍업(백필). 평가/주문 없이 IndicatorEngine만 채운다.

        일봉 전략은 워밍업에 60+봉이 필요하므로, 시작 시 히스토리를 주입하지 않으면
        수십 거래일 동안 관망만 하게 된다. run(feed) 전에 호출할 것.
        """
        for bar in sorted(bars, key=lambda b: b.ts):
            snap = self._engine.update(bar)
            self._prev[bar.ticker] = snap
            self._last_price[bar.ticker] = bar.close
        return len(bars)

    def make_fill_poller(self) -> FillPoller:
        """Ground-truth fill delivery: polls broker 체결내역 -> the composed fill handler."""
        return FillPoller(self._broker, self._sf, self._on_fill)

    async def reconcile(self) -> ReconcileReport:
        """Reconcile local open-position qty against the broker 잔고 (broker = source of truth)."""
        return await Reconciler(self._broker, self._sf).reconcile()

    async def run(self, feed: Feed) -> None:
        async for bar in feed.stream():
            await self.process(bar)

    async def process(self, bar: Bar) -> None:
        self._last_price[bar.ticker] = bar.close
        bar_day = bar.ts.date()
        if self._daily_date != bar_day:  # 새 거래일: 일일 실현손익 리셋
            self._daily_date = bar_day
            self._daily_realized = Decimal(0)
        # Kill switch: liquidate everything, then halt (no new entries/management).
        if self.control.flat_all_requested:
            await self._flat_all()
            self.control.clear_flat_all()

        snap = self._engine.update(bar)  # keep indicators warm even while halted
        ticker = bar.ticker
        if self.control.is_stopped:
            self._prev[ticker] = snap
            return

        lot = self._lots.get(ticker)
        if lot is not None and lot.is_open:
            lot.on_bar(bar.high)
        if (lot is None or lot.is_terminal) and ticker in self._watchlist:
            lot = await self._spawn(ticker)
        if lot is None:
            self._prev[ticker] = snap
            return

        equity = await self._equity()
        snapshot = self._risk_snapshot(equity)
        news = self._news_provider(ticker) if self._news_provider is not None else self._news
        for intent in lot.evaluate(snap, equity, prev=self._prev.get(ticker), news_ewma=news):
            if intent.is_actionable:
                await self._handle_intent(intent, lot, bar, snapshot)
        self._prev[ticker] = snap

    # -- intent handling -------------------------------------------------

    async def _handle_intent(
        self, intent: Intent, lot: PositionLot, bar: Bar, snapshot: RiskSnapshot
    ) -> None:
        qty = lot.qty if intent.kind is IntentKind.EXIT else intent.qty
        if qty is None and intent.fraction is not None:
            # Fraction-based TRIM: convert to an absolute quantity of the current position.
            raw = lot.qty * Decimal(str(intent.fraction))
            qty = raw if lot.market.is_overseas else raw.to_integral_value(rounding=ROUND_DOWN)
            qty = min(qty, lot.qty)
        if qty is None or qty <= 0:
            return
        decision = self._risk.check(
            intent_kind=intent.kind,
            ticker=lot.ticker,
            order_notional=bar.close * qty,
            snapshot=snapshot,
        )
        if not decision.allowed:
            await self._notifier.notify("intent.blocked", ticker=lot.ticker, reason=decision.reason)
            return
        await self._submit(
            lot, intent.side, qty, bar.close, is_add=intent.kind is IntentKind.ADD, reason=intent.reason
        )

    async def _submit(
        self, lot: PositionLot, side: Side, qty: Decimal, price: Decimal, *, is_add: bool, reason: str
    ) -> None:
        if qty <= 0:
            return
        cid = f"{lot.lot_id}-{self._counter}"
        self._counter += 1
        self._co_map[cid] = (lot, side, is_add)
        req = OrderRequest(
            client_order_id=cid,
            lot_id=lot.lot_id,
            ticker=lot.ticker,
            market=lot.market,
            side=side,
            qty=qty,
            price=price,  # marketable limit at last price
            ord_dvsn="00",
        )
        ack = await self._om.submit(req)
        await self._notifier.notify(
            "order.accepted" if ack.accepted else "order.rejected",
            ticker=lot.ticker,
            side=side.value,
            qty=str(qty),
            reason=reason,
        )

    async def _flat_all(self) -> None:
        for ticker, lot in list(self._lots.items()):
            if lot.is_open:
                price = self._last_price.get(ticker, lot.avg_entry)
                await self._submit(lot, Side.SELL, lot.qty, price, is_add=False, reason="kill_switch")
        await self._notifier.notify("kill_switch.flat_all")

    # -- fills (composed: persist + in-memory lot sync) ------------------

    async def _on_fill(self, fill: Fill) -> None:
        await self._om.handle_fill(fill)  # DB: order/fill/position projection + audit
        meta = self._co_map.get(fill.client_order_id)
        if meta is not None:
            lot, side, is_add = meta
            before = lot.realized_pnl
            lot.apply_fill(side, fill.qty, fill.price, fill.fee, fill.tax, is_add=is_add)
            self._daily_realized += lot.realized_pnl - before  # 당일 한도 계산용 (비용 포함)

    # -- helpers ---------------------------------------------------------

    async def _spawn(self, ticker: str) -> PositionLot:
        template = self._watchlist[ticker]
        lot = PositionFactory.create(Signal(ticker=ticker, market=template.market), template)
        self._lots[ticker] = lot
        async with session_scope(self._sf) as session:
            session.add(
                Position(
                    lot_id=lot.lot_id,
                    ticker=ticker,
                    market=lot.market.value,
                    currency=lot.currency.value,
                    side="BUY",
                    state=lot.state.value,
                    strategy_id=lot.params.strategy_id,
                    params_json=lot.params.model_dump(mode="json"),
                    resolution=lot.params.resolution.value,
                    qty_target=Decimal(0),
                )
            )
        return lot

    async def _equity(self) -> Decimal:
        balance = await self._broker.get_balance()
        equity = balance.cash.get(Currency.KRW, Decimal(0))
        for ticker, lot in self._lots.items():
            if lot.qty > 0:
                equity += lot.qty * self._last_price.get(ticker, lot.avg_entry)
        return equity

    def _risk_snapshot(self, equity: Decimal) -> RiskSnapshot:
        open_lots = [lot for lot in self._lots.values() if lot.is_open]
        exposure: dict[str, Decimal] = {}
        for lot in open_lots:
            price = self._last_price.get(lot.ticker, lot.avg_entry)
            exposure[lot.ticker] = exposure.get(lot.ticker, Decimal(0)) + lot.qty * price
        self._peak_equity = max(self._peak_equity, equity)
        return RiskSnapshot(
            equity=equity,
            open_positions=len(open_lots),
            daily_pnl=self._daily_realized,  # 당일 실현손익 (자정 리셋)
            ticker_exposure=exposure,
            peak_equity=self._peak_equity,
        )
