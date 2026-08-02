"""TradingService — the async engine orchestrator.

Wires the pipeline: Feed → IndicatorEngine → per-lot PositionLot.evaluate → RiskManager
gate → OrderManager (idempotent) → BrokerAdapter. Fills flow back through a composed handler
that persists (OrderManager) AND syncs the in-memory lot (strategy source of truth). Remote
control: PAUSE blocks new entries (risk gate); STOP requests a flat-all liquidation that the
loop executes promptly. The same service runs over a ReplayFeed (paper-over-history / tests)
or a live KIS WebSocket feed.

MULTI-RESOLUTION: lots are keyed ``ticker@resolution`` and a bar routes ONLY to lots of its
own resolution, so one process can run e.g. 1D 눌림목 + 60m 눌림목 + 5m ORB simultaneously —
including the same ticker on different timeframes (watchlist keys may be ``ticker@label``).
This matters because KIS allows a single concurrent WS connection per appkey.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as _date
from decimal import ROUND_DOWN, Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..domain.enums import Currency, Market, OrderState, PositionState, Side
from ..domain.factory import PositionFactory
from ..domain.params import PositionParams
from ..domain.position import PositionLot
from ..domain.signal import Intent, IntentKind, Signal
from ..execution.broker.base import BrokerAdapter
from ..execution.fill_poller import FillPoller
from ..execution.fx import FxRates
from ..execution.order_manager import OrderManager
from ..execution.reconciler import Reconciler, ReconcileReport
from ..execution.types import Fill, OrderRequest
from ..execution.unknown_resolver import UnknownOrderResolver
from ..infra.logging import get_logger
from ..infra.notifier.base import InMemoryNotifier, Notifier
from ..market.feed import Feed
from ..market.indicators import IndicatorEngine
from ..market.regime import MarketRegime
from ..market.types import Bar, IndicatorSnapshot
from ..persistence.db import session_scope
from ..persistence.models import Order, Position
from ..risk.limits import RiskSnapshot
from ..risk.manager import RiskManager
from ..strategy.registry import create_strategy
from ..strategy.templates import StrategyTemplate

_ENTRY_KINDS = (IntentKind.ENTER, IntentKind.ADD)

# An order in one of these states can no longer fill; its (lot, side) lock is releasable.
_TERMINAL_ORDER_STATES = frozenset(
    {
        OrderState.FILLED.value,
        OrderState.REJECTED.value,
        OrderState.CANCELLED.value,
        OrderState.EXPIRED.value,
    }
)

# Still (possibly) live at the broker: keep the duplicate-order lock across restarts.
_OPEN_ORDER_STATES = (
    OrderState.PENDING_NEW.value,
    OrderState.UNKNOWN.value,
    OrderState.NEW.value,
    OrderState.PARTIALLY_FILLED.value,
)


@dataclass(slots=True)
class _PendingOrder:
    lot: PositionLot
    side: Side
    is_add: bool
    requested_qty: Decimal
    filled_qty: Decimal = Decimal(0)


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
        fx_rates: FxRates | None = None,
        regime: MarketRegime | None = None,
    ) -> None:
        self._broker = broker
        self._sf = session_factory
        self._risk = risk
        self._watchlist = watchlist
        # config 키는 "TICKER" 또는 "TICKER@라벨" — 같은 종목을 여러 해상도로 운용 가능.
        self._by_ticker: dict[str, list[StrategyTemplate]] = {}
        seen: set[tuple[str, str]] = set()
        for key, template in watchlist.items():
            ticker = key.split("@")[0]
            pair = (ticker, template.resolution.value)
            if pair in seen:
                raise ValueError(f"duplicate watchlist entry for {ticker}@{template.resolution.value}")
            seen.add(pair)
            self._by_ticker.setdefault(ticker, []).append(template)
        self._om = OrderManager(broker, session_factory)
        self._engine = IndicatorEngine()
        self._notifier = notifier or InMemoryNotifier()
        self._news = news_ewma
        self._news_provider = news_provider  # per-ticker EWMA (overrides scalar news_ewma)
        self._log = get_logger("service")

        self._lots: dict[str, PositionLot] = {}
        self._prev: dict[str, IndicatorSnapshot] = {}
        self._last_price: dict[str, Decimal] = {}
        self._co_map: dict[str, _PendingOrder] = {}
        self._pending: dict[tuple[str, Side], str] = {}  # in-flight order per (lot, side)
        self._fx_rates = fx_rates or FxRates()
        self._fx_warned: set[Currency] = set()
        self._runtime_synced: dict[str, tuple[str, str, int, str]] = {}  # 마지막 영속 스냅샷
        # (ticker, resolution) -> 첫 봉 날짜. one-shot(스캐너 합류) 표시 + 랏 종료 후
        # 재스폰 금지 + 미진입 랏의 당일 만료 판정에 쓴다 (None = 아직 봉 못 봄).
        self._one_shot: dict[tuple[str, str], _date | None] = {}
        self._daily_realized = Decimal(0)  # 당일 실현손익 (수수료·세금 포함), 날짜 바뀌면 리셋
        self._daily_date: object | None = None
        self._peak_equity = Decimal(0)  # high-water mark (총 낙폭 브레이크 기준)
        self._regime = regime  # 시장 레짐 필터 (None = 미사용)
        self._regime_gated: set[tuple[str, str]] = {
            (key.split("@")[0], t.resolution.value)
            for key, t in watchlist.items()
            if t.regime_filter
        }

        # Override OrderManager's fill handler with a composed one (persist + lot sync).
        broker.fill_handler = self._on_fill
        self.control = risk.control

    @staticmethod
    def lot_key(ticker: str, resolution: object) -> str:
        return f"{ticker}@{getattr(resolution, 'value', resolution)}"

    @property
    def lots(self) -> dict[str, PositionLot]:
        """Keyed ``ticker@resolution`` (e.g. "005930@1D")."""
        return self._lots

    def lot(self, ticker: str, resolution: object | None = None) -> PositionLot | None:
        """Convenience lookup; without resolution returns the first lot for the ticker."""
        if resolution is not None:
            return self._lots.get(self.lot_key(ticker, resolution))
        for key, lot in self._lots.items():
            if key.split("@")[0] == ticker:
                return lot
        return None

    async def hydrate(self) -> int:
        """Rebuild in-memory lots from the DB Position projection (restart recovery).

        Restores qty/avg/realized/state plus the runtime stop state (initial_stop,
        peak_price, tp_rungs_taken, original_qty) that ``_sync_runtime`` persists, so a
        restarted lot keeps its stop ladder; reconcile against the broker before
        resuming trading.
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
            params = PositionParams(**row.params_json)
            key = self.lot_key(row.ticker, params.resolution)
            if key in self._lots:
                continue
            self._lots[key] = PositionLot(
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
                initial_stop=row.initial_stop if row.initial_stop > 0 else None,
                peak_price=row.peak_price if row.peak_price > 0 else row.avg_entry_price,
                tp_rungs_taken=row.tp_rungs_taken,
                # 구버전 행(컬럼 default 0)은 현재 보유량으로 폴백.
                original_qty=row.original_qty if row.original_qty > 0 else row.qty_filled,
            )
            restored += 1
        # Restore duplicate-order locks for orders that may still be live at the broker.
        # client_order_id is a fresh uuid per submit, so without this a restart would
        # happily re-order on the next signal while the pre-restart order still works.
        async with session_scope(self._sf) as session:
            open_orders = (
                await session.execute(select(Order).where(Order.state.in_(_OPEN_ORDER_STATES)))
            ).scalars().all()
        lot_ids = {lot.lot_id for lot in self._lots.values()}
        for order in open_orders:
            if order.lot_id in lot_ids:
                self._pending[(order.lot_id, Side(order.side))] = order.client_order_id
        return restored

    def prime(self, bars: list[Bar]) -> int:
        """과거 봉으로 지표 워밍업(백필). 평가/주문 없이 IndicatorEngine만 채운다.

        일봉 전략은 워밍업에 60+봉이 필요하므로, 시작 시 히스토리를 주입하지 않으면
        수십 거래일 동안 관망만 하게 된다. run(feed) 전에 호출할 것.
        """
        for bar in sorted(bars, key=lambda b: b.ts):
            snap = self._engine.update(bar)
            self._prev[self.lot_key(bar.ticker, bar.resolution)] = snap
            self._last_price[bar.ticker] = bar.close
        return len(bars)

    def add_template(self, key: str, template: StrategyTemplate, *, one_shot: bool = False) -> bool:
        """장중 스캐너의 동적 종목 합류: 워치리스트에 템플릿을 추가한다.

        다음 해당-해상도 봉이 오면 process()가 자동으로 랏을 스폰한다. 같은
        (종목, 해상도)가 이미 있으면 False (중복 운용 방지). WS 구독과 지표
        워밍업(prime)은 호출자 몫. ``one_shot=True``면 랏이 한 번 종료된 뒤
        재스폰하지 않고 템플릿을 제거한다 (급등주 재진입 churn 방지).
        """
        ticker = key.split("@")[0]
        for existing in self._by_ticker.get(ticker, []):
            if existing.resolution is template.resolution:
                return False
        self._watchlist[key] = template
        self._by_ticker.setdefault(ticker, []).append(template)
        if one_shot:
            self._one_shot[ticker, template.resolution.value] = None
        if template.regime_filter:
            self._regime_gated.add((ticker, template.resolution.value))
        self._log.info(
            "watchlist.joined", ticker=ticker, resolution=template.resolution.value,
            strategy=template.strategy_id, one_shot=one_shot,
        )
        return True

    def _remove_template(self, ticker: str, template: StrategyTemplate) -> None:
        templates = self._by_ticker.get(ticker, [])
        if template in templates:
            templates.remove(template)
        for key, tmpl in list(self._watchlist.items()):
            if key.split("@")[0] == ticker and tmpl is template:
                del self._watchlist[key]
        self._one_shot.pop((ticker, template.resolution.value), None)
        self._log.info("watchlist.retired", ticker=ticker, resolution=template.resolution.value)

    def open_tickers(self) -> set[str]:
        """현재 열린(청산 안 된) 랏들의 티커 — 재시작 시 WS 구독 목록에 포함해야 한다."""
        return {lot.ticker for lot in self._lots.values() if lot.is_open}

    def make_fill_poller(self) -> FillPoller:
        """Ground-truth fill delivery: polls broker 체결내역 -> the composed fill handler."""
        return FillPoller(self._broker, self._sf, self._on_fill)

    def make_unknown_resolver(self) -> UnknownOrderResolver:
        """Recovers submit-timeout UNKNOWN orders via the broker 일별주문내역."""
        return UnknownOrderResolver(self._broker, self._sf)

    async def reconcile(self) -> ReconcileReport:
        """Reconcile local open-position qty against the broker 잔고 (broker = source of truth)."""
        return await Reconciler(self._broker, self._sf).reconcile()

    async def run(self, feed: Feed) -> None:
        async for bar in feed.stream():
            await self.process(bar)

    async def process(self, bar: Bar) -> None:
        self._last_price[bar.ticker] = bar.close
        if self._regime is not None and bar.ticker == self._regime.proxy_ticker:
            self._regime.on_proxy_bar(bar.ts.date(), bar.close)
        bar_day = bar.ts.date()
        if self._daily_date != bar_day:  # 새 거래일: 일일 실현손익 리셋
            self._daily_date = bar_day
            self._daily_realized = Decimal(0)
        # Kill switch: liquidate everything, then halt (no new entries/management).
        if self.control.flat_all_requested:
            await self._flat_all()
            if not any(lot.qty > 0 for lot in self._lots.values()):
                self.control.clear_flat_all()

        snap = self._engine.update(bar)  # keep indicators warm even while halted
        ticker = bar.ticker
        pkey = self.lot_key(ticker, bar.resolution)  # prev/lot 모두 (종목, 해상도) 단위
        if self.control.is_stopped:
            self._prev[pkey] = snap
            return

        # 이 봉의 해상도에 해당하는 템플릿만 라우팅 (멀티 해상도 동시 운용의 핵심).
        template = next(
            (t for t in self._by_ticker.get(ticker, []) if t.resolution is bar.resolution), None
        )
        lot = self._lots.get(pkey)
        if template is not None and (os_key := (ticker, template.resolution.value)) in self._one_shot:
            joined_day = self._one_shot[os_key]
            if joined_day is None:
                self._one_shot[os_key] = bar_day  # 합류 후 첫 봉 = 합류 거래일
            elif (
                lot is not None
                and lot.state is PositionState.WATCHING
                and lot.qty == 0
                and bar_day > joined_day
            ):
                # 미진입 스캐너 랏의 당일 만료: 합류 근거(당일 급등+거래폭증)는 다음 날
                # 유효하지 않다. 검증된 시뮬레이션(당일 one-shot 리플레이)에는 없는
                # '며칠 뒤 진입'이 라이브에서 실측돼(7/23 합류→7/27 진입) 정렬한다.
                await self._expire_scan_lot(lot, template)
                self._prev[pkey] = snap
                return
        if (
            template is not None
            and lot is not None
            and lot.is_terminal
            and (ticker, template.resolution.value) in self._one_shot
        ):
            # 스캐너 합류분은 1회전 후 종료: 재스폰이 잔손실 반복(churn)을 만들었다.
            self._remove_template(ticker, template)
            template = None
        if template is not None and (lot is None or lot.is_terminal):
            lot = await self._spawn(ticker, template)
        if lot is None:  # 워치리스트 밖 + 복원 lot도 없음
            self._prev[pkey] = snap
            return

        if lot.is_open:
            lot.on_bar(bar.high)
        equity = await self._equity()
        snapshot = self._risk_snapshot(equity)
        news = self._news_provider(ticker) if self._news_provider is not None else self._news
        for intent in lot.evaluate(snap, equity, prev=self._prev.get(pkey), news_ewma=news):
            if intent.is_actionable:
                await self._handle_intent(intent, lot, bar, snapshot)
        await self._sync_runtime(lot)
        self._prev[pkey] = snap

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
        fx_rate = self._fx_rate_checked(lot.currency)
        if lot.currency is not Currency.KRW and fx_rate <= 0 and intent.kind in _ENTRY_KINDS:
            # Never open exposure we cannot value in KRW; risk-reducing EXIT/TRIM must
            # still go out (RiskManager always allows them, so the notional is unused).
            await self._notifier.notify("intent.blocked", ticker=lot.ticker, reason="missing_fx_rate")
            return
        if intent.kind in _ENTRY_KINDS:
            # 시장 레짐 필터: 시장 날씨가 나쁜 날은 신규 진입 금지 (청산은 무관).
            if (
                self._regime is not None
                and (lot.ticker, lot.params.resolution.value) in self._regime_gated
                and not self._regime.entries_allowed
            ):
                await self._notifier.notify(
                    "intent.blocked", ticker=lot.ticker, reason="market_regime"
                )
                return
            # 전략 사이징(risk_per_trade)이 max_order_notional을 넘으면 관망 대신 한도에
            # 맞춰 수량을 축소 진입한다 — 하드 블록이면 한도 < 사이징인 조합은 영원히
            # 매매가 없어 포워드 테스트가 공전한다.
            capped = self._cap_entry_qty(qty, lot, bar.close, fx_rate)
            if capped < qty:
                if intent.kind is IntentKind.ENTER:
                    lot.rebase_entry_qty(capped)  # TP 분할 기준도 실제 주문 수량으로
                await self._notifier.notify(
                    "intent.qty_capped",
                    ticker=lot.ticker,
                    requested=str(qty),
                    capped=str(capped),
                    reason="max_order_notional",
                )
            qty = capped
            if qty <= 0:
                return
        decision = self._risk.check(
            intent_kind=intent.kind,
            ticker=lot.ticker,
            order_notional=bar.close * qty * fx_rate,
            snapshot=snapshot,
        )
        if not decision.allowed:
            await self._notifier.notify("intent.blocked", ticker=lot.ticker, reason=decision.reason)
            return
        submitted = await self._submit(
            lot, intent.side, qty, bar.close, is_add=intent.kind is IntentKind.ADD,
            reason=intent.reason,
            # EXIT(손절·세션청산·킬스위치)는 걸려 있는 같은 방향 주문(TP 트림 등)을
            # 취소하고라도 나가야 한다 — 익절 주문이 손절을 막는 사고 방지.
            replace_pending=intent.kind is IntentKind.EXIT,
        )
        if (
            not submitted
            and intent.kind is IntentKind.TRIM
            and intent.reason.startswith("take_profit")
        ):
            # The TP ladder advances at emit time (PositionLot.evaluate); if the order
            # never went out, give the rung back so the trim can re-fire on a later bar.
            lot.tp_rungs_taken = max(0, lot.tp_rungs_taken - 1)

    async def _submit(
        self,
        lot: PositionLot,
        side: Side,
        qty: Decimal,
        price: Decimal,
        *,
        is_add: bool,
        reason: str,
        replace_pending: bool = False,
    ) -> bool:
        """Place one order per (lot, side) at a time; returns True when accepted.

        ``replace_pending=True``(EXIT 전용): 같은 (lot, side)의 미체결 주문을 브로커에
        취소 요청하고 성공하면 이어서 제출한다. 취소가 확인되지 않으면 제출하지 않는다
        (이중 매도 방지) — UNKNOWN 주문은 resolver가 정체를 밝힐 때까지 잠금 유지."""
        if qty <= 0:
            return False
        pending_key = (lot.lot_id, side)
        if pending_key in self._pending and not await self._release_if_terminal(pending_key):
            if not replace_pending:
                self._log.info("order.pending.skip", lot_id=lot.lot_id, side=side.value)
                return False
            if not await self._cancel_pending(pending_key, lot):
                self._log.warning("order.replace.cancel_failed", lot_id=lot.lot_id)
                return False
        cid = f"{lot.lot_id}-{uuid4().hex}"
        self._pending[pending_key] = cid
        self._co_map[cid] = _PendingOrder(lot, side, is_add, qty)
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
        try:
            ack = await self._om.submit(req)
        except Exception:
            # Keep UNKNOWN broker outcomes locked: resubmitting could duplicate a live order.
            self._log.exception("order.submit.unknown", client_order_id=cid)
            raise
        if not ack.accepted:
            self._pending.pop(pending_key, None)
            self._co_map.pop(cid, None)
        await self._notifier.notify(
            "order.accepted" if ack.accepted else "order.rejected",
            ticker=lot.ticker,
            side=side.value,
            qty=str(qty),
            reason=reason,
        )
        return ack.accepted

    async def _cancel_pending(self, pending_key: tuple[str, Side], lot: PositionLot) -> bool:
        """미체결 (lot, side) 주문을 취소하고 락을 푼다. 실패 시 False (락 유지)."""
        cid = self._pending.get(pending_key)
        if cid is None:
            return True
        po = self._co_map.get(cid)
        req = OrderRequest(
            client_order_id=cid,
            lot_id=lot.lot_id,
            ticker=lot.ticker,
            market=lot.market,
            side=pending_key[1],
            qty=po.requested_qty if po is not None else Decimal(0),
            price=Decimal(0),
            ord_dvsn="00",
        )
        if not await self._om.cancel(req):
            return False
        self._pending.pop(pending_key, None)
        self._co_map.pop(cid, None)
        await self._notifier.notify("order.cancelled", ticker=lot.ticker, reason="replaced_by_exit")
        return True

    async def _release_if_terminal(self, pending_key: tuple[str, Side]) -> bool:
        """Free a pending lock whose order can no longer fill (per the DB order state).

        Fills release locks in ``_on_fill``, but cancel/expiry never produce a fill —
        without this check one partially-filled-then-expired order would block the
        (lot, side) forever, including stop-loss and kill-switch sells. UNKNOWN and
        working orders stay locked: they may still be live at the broker.
        """
        cid = self._pending.get(pending_key)
        if cid is None:
            return True
        async with session_scope(self._sf) as session:
            state = (
                await session.execute(select(Order.state).where(Order.client_order_id == cid))
            ).scalar_one_or_none()
        if state is None or state in _TERMINAL_ORDER_STATES:
            self._pending.pop(pending_key, None)
            self._co_map.pop(cid, None)
            return True
        return False

    async def _flat_all(self) -> None:
        submitted = False
        for lot in list(self._lots.values()):
            if lot.is_open:
                price = self._last_price.get(lot.ticker, lot.avg_entry)
                ok = await self._submit(lot, Side.SELL, lot.qty, price, is_add=False, reason="kill_switch")
                submitted = submitted or ok
        # flat_all re-runs every bar until all lots are flat; only notify when this pass
        # actually placed an order (otherwise the notifier is spammed once per bar).
        if submitted:
            await self._notifier.notify("kill_switch.flat_all")

    # -- fills (composed: persist + in-memory lot sync) ------------------

    async def _on_fill(self, fill: Fill) -> None:
        await self._om.handle_fill(fill)  # DB: order/fill/position projection + audit
        meta = self._co_map.get(fill.client_order_id)
        if meta is not None:
            lot, side, is_add = meta.lot, meta.side, meta.is_add
            before = lot.realized_pnl
            lot.apply_fill(side, fill.qty, fill.price, fill.fee, fill.tax, is_add=is_add)
            self._daily_realized += lot.realized_pnl - before  # 당일 한도 계산용 (비용 포함)
            await self._sync_runtime(lot)  # 진입 체결로 확정된 initial_stop/original_qty 즉시 영속
            meta.filled_qty += fill.qty
            if meta.filled_qty >= meta.requested_qty:
                if self._pending.get((lot.lot_id, side)) == fill.client_order_id:
                    self._pending.pop((lot.lot_id, side), None)
                self._co_map.pop(fill.client_order_id, None)

    # -- helpers ---------------------------------------------------------

    async def _spawn(self, ticker: str, template: StrategyTemplate) -> PositionLot:
        lot = PositionFactory.create(Signal(ticker=ticker, market=template.market), template)
        self._lots[self.lot_key(ticker, template.resolution)] = lot
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

    async def _expire_scan_lot(self, lot: PositionLot, template: StrategyTemplate) -> None:
        """미진입 one-shot(스캐너) 랏을 CANCELLED로 은퇴시키고 DB 투영도 종결한다."""
        lot.transition_to(PositionState.CANCELLED)
        async with session_scope(self._sf) as session:
            row = await session.get(Position, lot.lot_id)
            if row is not None:
                row.state = lot.state.value
                row.closed_at = datetime.now(UTC)
        self._lots.pop(self.lot_key(lot.ticker, lot.params.resolution), None)
        self._remove_template(lot.ticker, template)
        self._log.info("scan_lot.expired", ticker=lot.ticker, lot_id=lot.lot_id)

    async def _sync_runtime(self, lot: PositionLot) -> None:
        """Persist the lot's runtime stop state so hydrate() can restore it after a restart."""
        snapshot = (
            str(lot.initial_stop),
            str(lot.peak_price),
            lot.tp_rungs_taken,
            str(lot.original_qty),
        )
        if self._runtime_synced.get(lot.lot_id) == snapshot:
            return
        async with session_scope(self._sf) as session:
            row = await session.get(Position, lot.lot_id)
            if row is None:
                return
            row.initial_stop = lot.initial_stop if lot.initial_stop is not None else Decimal(0)
            row.peak_price = lot.peak_price
            row.tp_rungs_taken = lot.tp_rungs_taken
            row.original_qty = lot.original_qty
        self._runtime_synced[lot.lot_id] = snapshot

    def _cap_entry_qty(
        self, qty: Decimal, lot: PositionLot, price: Decimal, fx_rate: Decimal
    ) -> Decimal:
        """Shrink an ENTER/ADD qty so its notional fits max_order_notional (0 if unfit)."""
        cap = self._risk.limits.max_order_notional
        if cap is None or price <= 0 or fx_rate <= 0:
            return qty
        if price * qty * fx_rate <= cap:
            return qty
        capped = cap / (price * fx_rate)
        if not lot.market.is_overseas:
            capped = capped.to_integral_value(rounding=ROUND_DOWN)
        return max(capped, Decimal(0))

    def _fx_rate_checked(self, currency: Currency) -> Decimal:
        """KRW rate for valuation; a missing rate is warned once and valued at 0.

        Raising here would kill the whole bar loop (cli reconnects forever) over e.g.
        USD dust in a 통합증거금 balance. Valuing at 0 understates equity (conservative
        for sizing) and entries in that currency are blocked in ``_handle_intent``.
        """
        rate = self._fx_rates.rate(currency)
        if currency is not Currency.KRW and rate <= 0 and currency not in self._fx_warned:
            self._fx_warned.add(currency)
            self._log.warning("fx.missing_rate", currency=currency.value)
        return rate

    async def _equity(self) -> Decimal:
        balance = await self._broker.get_balance()
        equity = Decimal(0)
        for currency, amount in balance.cash.items():
            equity += amount * self._fx_rate_checked(currency)
        for lot in self._lots.values():
            if lot.qty > 0:
                rate = self._fx_rate_checked(lot.currency)
                equity += lot.qty * self._last_price.get(lot.ticker, lot.avg_entry) * rate
        return equity

    def _risk_snapshot(self, equity: Decimal) -> RiskSnapshot:
        open_lots = [lot for lot in self._lots.values() if lot.is_open]
        exposure: dict[str, Decimal] = {}
        for lot in open_lots:
            price = self._last_price.get(lot.ticker, lot.avg_entry)
            rate = self._fx_rate_checked(lot.currency)
            exposure[lot.ticker] = exposure.get(lot.ticker, Decimal(0)) + lot.qty * price * rate
        self._peak_equity = max(self._peak_equity, equity)
        return RiskSnapshot(
            equity=equity,
            open_positions=len(open_lots),
            daily_pnl=self._daily_realized,  # 당일 실현손익 (자정 리셋)
            ticker_exposure=exposure,
            peak_equity=self._peak_equity,
        )
