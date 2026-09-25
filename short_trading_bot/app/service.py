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

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
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
from ..execution.tick_size import round_to_tick
from ..execution.types import Fill, OrderRequest
from ..execution.unknown_resolver import UnknownOrderResolver
from ..infra.logging import get_logger
from ..infra.notifier.base import InMemoryNotifier, Notifier
from ..market.feed import Feed
from ..market.indicators import IndicatorEngine
from ..market.regime import MarketRegime
from ..market.types import Bar, IndicatorSnapshot
from ..persistence.db import session_scope
from ..persistence.models import AuditLog, Order, Position
from ..persistence.models import Fill as FillRow
from ..risk.limits import RiskSnapshot
from ..risk.manager import RiskManager
from ..strategy.registry import create_strategy
from ..strategy.templates import StrategyTemplate
from .health import EngineHealth

_ENTRY_KINDS = (IntentKind.ENTER, IntentKind.ADD)
_KST = timezone(timedelta(hours=9))
# 최고 평가금(낙폭 브레이크 기준) 영속 간격: 직전 기록 대비 0.1% 이상 올랐을 때만 audit 에 쓴다.
_PEAK_PERSIST_STEP = Decimal("1.001")

# 대시보드 스냅샷은 5초마다 쓰이지만 잔고 REST 조회는 그보다 훨씬 느리게 해도 된다.
# 24시간 5초 주기 = 하루 ~17,000회로 KIS 초당 한도를 갉아먹어 체결 조회를 밀어낸다.
EQUITY_TTL_SECONDS = 30.0

# An order in one of these states can no longer fill; its (lot, side) lock is releasable.
_TERMINAL_ORDER_STATES = frozenset(
    {
        OrderState.FILLED.value,
        OrderState.REJECTED.value,
        OrderState.CANCELLED.value,
        OrderState.EXPIRED.value,
    }
)

_OPEN_POSITION_STATES = frozenset(
    {PositionState.HOLDING.value, PositionState.SCALING.value, PositionState.EXITING.value}
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
    price: Decimal = Decimal(0)  # 주문 지정가 (킬스위치 재가격 판단용)


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
        # lot_id -> 랏 (종료·교체된 랏 포함). 재시작/취소 뒤 도착한 체결을 올바른 메모리 랏에
        # 반영하려면 키(종목@해상도) 슬롯이 새 랏으로 바뀐 뒤에도 원래 랏을 찾아야 한다.
        self._lots_by_id: dict[str, PositionLot] = {}
        # 교체 매도의 체결 반영이 실패한 랏 — 반영 성공 전까지 매도 재제출을 보류한다.
        self._refresh_before_sell: set[str] = set()
        # 매수 취소는 됐지만 취소 직전 체결분 반영(폴링)이 아직 확인 안 된 랏. 비어 있어야 flat.
        self._unconfirmed_buy_cancels: set[str] = set()
        # 재시작 직후 체결 반영 전. 위 두 표시는 메모리 전용이라 '취소는 됐는데 반영 전' 상태로
        # 죽으면 사라진다 — 그래서 재시작 후 첫 폴링이 성공할 때까지 같은 장벽을 전역으로 친다.
        self._startup_refresh_pending = False
        self._prev: dict[str, IndicatorSnapshot] = {}
        self._last_price: dict[str, Decimal] = {}
        # "살아는 있는데 일을 하나" 판정용 (시세 무소식·폴링 실패). 대시보드/알림 공용.
        self.health = EngineHealth()
        self._equity_cache: Decimal | None = None
        self._equity_at = 0.0  # monotonic
        self._co_map: dict[str, _PendingOrder] = {}
        self._fill_poller: FillPoller | None = None
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
        self._peak_persisted = Decimal(0)
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
        # 브로커 호출 도중 죽은 흔적(PENDING_NEW)은 UNKNOWN 으로 넘겨 resolver 가 밝히게 하고,
        # 전 거래일 주문은 만료 — 둘 다 안 하면 해당 (lot, side) 잠금이 영구히 남는다 (#6).
        await self._om.orphan_pending_to_unknown()
        await self._om.expire_stale()
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
                # 같은 슬롯에 열린 행이 둘(재오픈된 옛 랏 + 새 랏)일 수 있다. 슬롯엔 하나만 들어가도
                # 보유는 전부 추적돼야 긴급중지가 둘 다 청산한다 — id 인덱스에는 반드시 넣는다.
                if row.lot_id not in self._lots_by_id and self._lots[key].lot_id != row.lot_id:
                    self._lots_by_id[row.lot_id] = self._lot_from_row(row)
                    self._log.warning("hydrate.slot_collision", lot_id=row.lot_id, key=key)
                continue
            lot = self._lot_from_row(row)
            self._lots[key] = lot
            self._lots_by_id[row.lot_id] = lot
            restored += 1
        await self._restore_risk_state()
        # Restore duplicate-order locks for orders that may still be live at the broker.
        # client_order_id is a fresh uuid per submit, so without this a restart would
        # happily re-order on the next signal while the pre-restart order still works.
        async with session_scope(self._sf) as session:
            open_orders = (
                await session.execute(select(Order).where(Order.state.in_(_OPEN_ORDER_STATES)))
            ).scalars().all()
            for order in open_orders:
                owner = self._lots_by_id.get(order.lot_id)
                if owner is None:
                    # 이미 CLOSED/CANCELLED 된 포지션의 주문이 아직 브로커에 살아 있을 수 있다
                    # (청산 중 매수 취소 실패 등). 버리면 잠금·메타가 없어 is_flat() 이 참이 되고
                    # 긴급중지 엔진이 먼저 종료 → 나중에 체결된 주식이 방치된다. 랏을 되살려
                    # (슬롯 밖, lot_id 로만) 잠금과 메타를 복원한다 — 체결되면 _on_fill 이 재오픈.
                    closed_row = await session.get(Position, order.lot_id)
                    if closed_row is None:
                        continue
                    owner = self._lot_from_row(closed_row)
                    self._lots_by_id[order.lot_id] = owner
                    self._log.warning(
                        "hydrate.open_order_on_closed_lot",
                        lot_id=order.lot_id, client_order_id=order.client_order_id,
                    )
                self._pending[(order.lot_id, Side(order.side))] = order.client_order_id
                # 체결 → 메모리 랏 반영에 필요한 메타도 복원한다. 잠금만 복원하면 재시작 뒤
                # 도착한 체결이 DB 에만 반영되고 메모리 랏은 WATCHING/0 으로 남아 같은 종목을
                # 또 사고, 이미 산 주식은 손절 대상에서 빠진다 (#3).
                self._co_map[order.client_order_id] = await self._meta_from_order(
                    session, order, owner
                )
        # 다운타임 동안의 체결(취소 직전 체결분 포함)을 먼저 반영한다. 실패하면 장벽이 남아
        # 매도 재제출·flat 판정이 반영 성공까지 보류된다.
        self._startup_refresh_pending = True
        await self._refresh_fills()
        return restored

    @staticmethod
    def _lot_from_row(row: Position) -> PositionLot:
        """DB Position 투영 → 메모리 랏 (재시작 복원·지연 체결로 재오픈된 랏 공용)."""
        params = PositionParams(**row.params_json)
        lot = PositionLot(
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
            # WATCHING 행의 initial_stop 은 '진입 대기 중 손절가' (아래에서 pending 으로 복원).
            initial_stop=(
                row.initial_stop
                if row.initial_stop > 0 and row.state != PositionState.WATCHING.value
                else None
            ),
            peak_price=row.peak_price if row.peak_price > 0 else row.avg_entry_price,
            tp_rungs_taken=row.tp_rungs_taken,
            # 구버전 행(컬럼 default 0)은 현재 보유량으로 폴백.
            original_qty=row.original_qty if row.original_qty > 0 else row.qty_filled,
        )
        if lot.state is PositionState.WATCHING:
            # 진입 주문이 걸린 채 재시작: 체결 시 적용할 손절가·의도 수량을 되살린다
            # (없으면 재시작 뒤 체결된 보유분이 전략 손절가 없이 관리된다).
            lot.restore_pending_entry(
                row.initial_stop if row.initial_stop > 0 else None,
                row.original_qty if row.original_qty > 0 else None,
            )
        return lot

    async def _restore_risk_state(self) -> None:
        """재시작해도 일일 손실 한도·낙폭 브레이크가 초기화되지 않게 (#10).

        - 당일 실현손익: 오늘(KST) 매도 체결이 있는 랏의 체결 전체를 시간순으로 재생해
          ``PositionLot.apply_fill`` 과 같은 산식으로 다시 계산한다 (비용 포함).
        - 최고 평가금: audit_log 의 마지막 ``risk.peak_equity`` 기록.
        메모리 전용이던 시절엔 한도 도달 → 크래시 → 워치독 재기동이면 진입이 다시 열렸다."""
        today = datetime.now(_KST).date()
        async with session_scope(self._sf) as session:
            rows = (
                await session.execute(
                    select(FillRow, Order.side)
                    .join(Order, FillRow.order_id == Order.order_id)
                    .order_by(FillRow.lot_id, FillRow.filled_at, FillRow.fill_id)
                )
            ).all()
            peak_row = (
                await session.execute(
                    select(AuditLog.payload_json)
                    .where(AuditLog.event_type == "risk.peak_equity")
                    .order_by(AuditLog.seq.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        lots_sold_today = {
            fill.lot_id for fill, side in rows if side == Side.SELL.value and _kst_day(fill.filled_at) == today
        }
        realized = Decimal(0)
        book: dict[str, tuple[Decimal, Decimal]] = {}  # lot_id -> (qty, avg)
        for fill, side in rows:
            if fill.lot_id not in lots_sold_today:
                continue
            qty, avg = book.get(fill.lot_id, (Decimal(0), Decimal(0)))
            if side == Side.BUY.value:
                new_qty = qty + fill.qty
                avg = (avg * qty + fill.price * fill.qty) / new_qty if new_qty > 0 else avg
                qty = new_qty
            else:
                held = min(fill.qty, qty) if qty > 0 else Decimal(0)
                if _kst_day(fill.filled_at) == today:
                    realized += (fill.price - avg) * held - fill.fee - fill.tax
                qty = max(Decimal(0), qty - fill.qty)
            book[fill.lot_id] = (qty, avg)
        self._daily_date = today
        self._daily_realized = realized
        # 시뮬 체결(PaperBroker)은 현금이 프로세스 메모리에만 있어 재시작마다 초기 자금으로
        # 돌아간다 — 이전 실행의 최고 평가금과 비교하면 가짜 낙폭이 된다. 실브로커만 복원.
        if self._broker.name == "paper":
            peak_row = None
        if isinstance(peak_row, dict) and peak_row.get("peak"):
            self._peak_equity = self._peak_persisted = Decimal(str(peak_row["peak"]))
        if realized or self._peak_equity:
            self._log.info(
                "risk_state.restored", daily_realized=str(realized), peak_equity=str(self._peak_equity)
            )

    async def _persist_peak(self) -> None:
        if self._broker.name == "paper":
            return  # 시뮬 현금은 재시작 시 초기화 — 영속해도 의미가 없다 (_restore_risk_state)
        if self._peak_equity <= 0 or self._peak_equity < self._peak_persisted * _PEAK_PERSIST_STEP:
            return
        async with session_scope(self._sf) as session:
            session.add(
                AuditLog(event_type="risk.peak_equity", payload_json={"peak": str(self._peak_equity)})
            )
        self._peak_persisted = self._peak_equity

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

    def tracked_tickers(self) -> set[str]:
        """전략이 붙어 있거나 랏이 살아 있는 모든 티커 = WS 구독 대상.

        WS 재접속은 매번 새 연결이라 구독을 다시 걸어야 하는데, 그 목록을 정적 리스트로
        들고 있으면 스캐너로 합류한 종목이 재접속 순간 시세를 잃는다 (2026-08-10 실사고:
        유니켐 09:06 합류 → 09:12 재접속에서 유실 → 6시간 반 깜깜이 → 진입 불가).
        반대로 합류분을 리스트에 계속 쌓기만 하면 만료된 종목이 안 빠져 구독 한도(~41)를
        채운다. 그래서 '지금 실제로 필요한 목록' 을 서비스 상태에서 매번 파생시킨다.

        **보유(open)가 아니라 살아 있는 랏 전부**를 넣는다. 관망(WATCHING) 랏이야말로
        진입 판단에 봉이 필요하고, 시세가 끊기면 당일 만료 판정(`_expire_scan_lot`)조차
        봉이 없어 못 돌아 랏이 영구히 남는 악순환이 생긴다 (실측: 관망 12 중 6이 미구독
        상태로 잔존). 구독이 붙으면 만료가 정상 작동해 스스로 정리된다."""
        lot_tickers = {lot.ticker for lot in self._lots.values()}
        return {ticker for ticker, tmpls in self._by_ticker.items() if tmpls} | lot_tickers

    async def status_snapshot(self) -> dict[str, object]:
        """대시보드용 실시간 현황 — 엔진이 주기적으로 파일에 기록해 API 프로세스가 읽는다.

        equity(브로커 잔고 조회)는 일시 실패해도 나머지 현황은 제공한다 (None 표기).

        잔고는 캐시한다: 스냅샷은 5초마다 쓰이지만 잔고 조회는 REST 왕복이라 하루 약
        17,000회가 되고(24시간), KIS 초당 한도를 갉아먹어 체결 조회 같은 필수 호출의
        실패율을 올린다. 평가금은 보유 랏 시가평가가 대부분이라 30초 캐시로 충분하다."""
        equity = await self._cached_equity()
        open_lots: list[dict[str, object]] = []
        watching: list[dict[str, object]] = []
        for lot in sorted(self._lots.values(), key=lambda x: (x.ticker, x.params.resolution.value)):
            last = self._last_price.get(lot.ticker)
            if lot.qty > 0:
                open_lots.append({
                    "ticker": lot.ticker,
                    "resolution": lot.params.resolution.value,
                    "strategy": lot.params.strategy_id,
                    "state": lot.state.value,
                    "qty": str(lot.qty),
                    "avg_entry": str(lot.avg_entry),
                    "last_price": str(last) if last is not None else None,
                    "unrealized": str((last - lot.avg_entry) * lot.qty) if last is not None else None,
                    "initial_stop": str(lot.initial_stop) if lot.initial_stop is not None else None,
                })
            elif lot.state is PositionState.WATCHING:
                watching.append({
                    "ticker": lot.ticker,
                    "resolution": lot.params.resolution.value,
                    "strategy": lot.params.strategy_id,
                })
        return {
            "ts": datetime.now(UTC).isoformat(),
            "control": self.control.state.value,
            "equity": str(equity) if equity is not None else None,
            "peak_equity": str(self._peak_equity),
            "daily_realized": str(self._daily_realized),
            "daily_date": str(self._daily_date) if self._daily_date is not None else None,
            "open_lots": open_lots,
            "watching": watching,
            "health": self.health.snapshot(datetime.now(UTC)),
        }

    def _tracked_lots(self) -> list[PositionLot]:
        """관리 슬롯의 랏 + 슬롯 밖 랏(재오픈됐는데 슬롯이 점유된 고아 등) — 중복 없이."""
        out: list[PositionLot] = []
        seen: set[int] = set()
        for lot in [*self._lots.values(), *self._lots_by_id.values()]:
            if id(lot) not in seen:
                seen.add(id(lot))
                out.append(lot)
        return out

    def is_flat(self) -> bool:
        """보유도, 걸린 주문도, 미확인 취소 체결도 없다 — 긴급중지 후 엔진이 종료해도 되는 조건.

        슬롯 밖 랏까지 본다: 슬롯이 점유돼 관리 슬롯에 못 들어간 재오픈 랏의 보유를 빼먹으면
        새 랏만 청산하고 flat 으로 판정해 종료한다."""
        return (
            not any(lot.qty > 0 for lot in self._tracked_lots())
            and not self._pending
            and not self._unconfirmed_buy_cancels
            and not self._startup_refresh_pending
        )

    async def expire_stale_orders(self) -> int:
        """거래일이 바뀌면 호출 — 전일 미체결 주문을 만료시켜 잠금이 풀리게 한다 (#6)."""
        return await self._om.expire_stale()

    def make_fill_poller(self) -> FillPoller:
        """Ground-truth fill delivery: polls broker 체결내역 -> the composed fill handler.

        프로세스에 하나만 둔다 — 백그라운드 폴링·재접속 복구·주문 교체 직전 갱신이 동시에
        돌면 같은 체결 델타를 두 번 반영할 수 있어, 한 인스턴스의 락으로 직렬화한다."""
        if self._fill_poller is None:
            self._fill_poller = FillPoller(self._broker, self._sf, self._on_fill)
        return self._fill_poller

    def make_unknown_resolver(self) -> UnknownOrderResolver:
        """Recovers submit-timeout UNKNOWN orders via the broker 일별주문내역."""
        return UnknownOrderResolver(self._broker, self._sf)

    async def reconcile(self) -> ReconcileReport:
        """Reconcile local open-position qty against the broker 잔고 (broker = source of truth)."""
        return await Reconciler(self._broker, self._sf).reconcile()

    async def run(self, feed: Feed) -> None:
        async for bar in feed.stream():
            try:
                await self.process(bar)
            except Exception:
                # 봉 1개의 처리 실패(대개 REST 일시 장애)가 시세 연결을 끊으면 안 된다.
                # 끊기면 재접속 5초 동안 **모든 종목**의 관리가 멈추고, 그 사이 손절·익절
                # 신호도 놓친다 — 국소 실패가 전면 중단으로 증폭되는 구조였다.
                # 2026-08-10 실측: 재접속 219회 중 194회가 이 경로(DNS/REST 오류)였다.
                self.health.on_process_error()
                self._log.exception("process.error", ticker=bar.ticker)

    async def process(self, bar: Bar) -> None:
        self._last_price[bar.ticker] = bar.close
        # 페이퍼 대기 지정가 매칭 — 봉의 저가/고가까지 넘겨 봉 중간 체결도 반영한다.
        await self._broker.on_market_price(bar.ticker, bar.close, low=bar.low, high=bar.high)
        self.health.on_bar(bar.ticker, datetime.now(UTC))
        if self._regime is not None and bar.ticker == self._regime.proxy_ticker:
            self._regime.on_proxy_bar(bar.ts.date(), bar.close)
        bar_day = bar.ts.date()
        if self._daily_date != bar_day:  # 새 거래일: 일일 실현손익 리셋
            self._daily_date = bar_day
            self._daily_realized = Decimal(0)
        # Kill switch: liquidate everything, then halt (no new entries/management).
        if self.control.flat_all_requested:
            await self._flat_all()
            # 보유 0 만 보고 해제하면 안 된다: UNKNOWN 매수가 남아 있다가 나중에 체결되면
            # 청산 요청 없이 STOPPED 로 남아 새 주식이 방치된다. 걸린 주문까지 0 일 때만 해제.
            if self.is_flat():
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
        # 잔고 조회(REST)는 캐시한다: 봉마다 조회하면 초당 한도를 잠식하고, 조회가 한 번
        # 실패하면 그 봉의 손절·트레일링 평가 전체가 예외로 건너뛰어졌다 (#8). 값이 아예
        # 없으면 0 으로 평가 — 청산 판단엔 영향이 없고 진입은 사이징·한도에서 막힌다.
        cached = await self._cached_equity()
        equity = cached if cached is not None else Decimal(0)
        snapshot = self._risk_snapshot(equity)
        await self._persist_peak()
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
        # 주문 가격은 **사이징 전에** 호가단위로 정렬한다. 정렬을 주문 직전에 하면
        # 매수 올림 때문에 qty x price 가 max_order_notional 을 넘어 한도가 깨진다.
        order_px = bar.close if lot.market.is_overseas else round_to_tick(bar.close, intent.side)
        if intent.kind in _ENTRY_KINDS:
            if (lot.lot_id, Side.BUY) in self._pending and not await self._release_if_terminal(
                (lot.lot_id, Side.BUY)
            ):
                # 이 랏의 매수가 이미 걸려 있다 — 아래 한도 검사는 그 미체결을 포지션으로 세므로
                # 여기서 조용히 건너뛴다 (매 봉 'intent.blocked' 알림 스팸 방지).
                self._log.info("order.pending.skip", lot_id=lot.lot_id, side=Side.BUY.value)
                return
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
            capped = self._cap_entry_qty(qty, lot, order_px, fx_rate)
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
            order_notional=order_px * qty * fx_rate,
            snapshot=snapshot,
        )
        if not decision.allowed:
            await self._notifier.notify("intent.blocked", ticker=lot.ticker, reason=decision.reason)
            return
        if intent.kind is IntentKind.EXIT:
            await self._cancel_open_buy(lot, reason="exit_cancels_entry")
            # 취소 뒤 체결 반영으로 보유가 늘었을 수 있다 — 전량 청산은 반영 후 수량으로.
            qty = lot.qty
            if qty <= 0:
                return
        submitted = await self._submit(
            lot, intent.side, qty, order_px, is_add=intent.kind is IntentKind.ADD,
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
        if side is Side.SELL and (lot.lot_id in self._refresh_before_sell or self._startup_refresh_pending):
            # 직전 교체 매도에서 취소는 됐지만 체결 반영(폴링)이 실패했다 — 취소된 주문의 미반영
            # 체결분이 있을 수 있으니, 반영에 성공하기 전엔 다시 내지 않는다 (과매도·거부 방지).
            if not await self._refresh_fills(lot):
                return False
            qty = min(qty, lot.qty)
            if qty <= 0:
                return False
        if pending_key in self._pending and not await self._release_if_terminal(pending_key):
            if not replace_pending:
                self._log.info("order.pending.skip", lot_id=lot.lot_id, side=side.value)
                return False
            if not await self._cancel_pending(pending_key, lot):
                self._log.warning("order.replace.cancel_failed", lot_id=lot.lot_id)
                return False
            if side is Side.SELL:
                # 취소된 주문이 취소 전에 일부 체결됐을 수 있다 (다음 폴링 전이라 메모리엔 아직
                # 없음). 그대로 원래 수량을 다시 내면 체결분+새 주문이 보유를 넘어 거부되거나
                # 과매도된다 → 체결내역을 한 번 반영한 뒤 실제 남은 보유만큼만 낸다.
                self._refresh_before_sell.add(lot.lot_id)
                if not await self._refresh_fills(lot):
                    # 반영 실패 시 메모리 보유량(lot.qty)을 믿을 수 없다 — 이번엔 내지 않고 다음
                    # 봉에서 반영부터 다시 시도한다. 취소는 이미 됐으므로 잠금도 풀려 있다.
                    return False
                qty = min(qty, lot.qty)
                if qty <= 0:
                    return False
        cid = f"{lot.lot_id}-{uuid4().hex}"
        # marketable limit at last price. 호가단위로 정렬한다 — 시세를 못 받아
        # 평단가(소수점)로 폴백하면 "호가단위 오류" 로 거부돼 손절이 막힌다.
        limit_px = round_to_tick(price, side) if not lot.market.is_overseas else price
        self._pending[pending_key] = cid
        self._co_map[cid] = _PendingOrder(lot, side, is_add, qty, price=limit_px)
        req = OrderRequest(
            client_order_id=cid,
            lot_id=lot.lot_id,
            ticker=lot.ticker,
            market=lot.market,
            side=side,
            qty=qty,
            price=limit_px,
            ord_dvsn="00",
        )
        try:
            ack = await self._om.submit(req)
        except Exception:
            # Keep UNKNOWN broker outcomes locked: resubmitting could duplicate a live order.
            self._log.exception("order.submit.unknown", client_order_id=cid)
            # UNKNOWN 동안 이 (lot, side) 는 취소·재주문이 막힌다 — 매도면 손절이 멈춘 것.
            await self._notifier.notify(
                "order.unknown", ticker=lot.ticker, side=side.value, qty=str(qty), reason=reason
            )
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

    async def _refresh_fills(self, lot: PositionLot | None = None) -> bool:
        """체결내역을 한 번 반영한다. 한 번의 폴링이 모든 랏을 갱신하므로 성공하면 모든
        '재반영 필요' 표시를 지운다."""
        # 반영 '확인'이 필요한 호출이다 — 라우팅 어댑터가 연속 실패로 해외 조회를 쉬고 있으면
        # 이번엔 다시 시도하게 한다. 안 그러면 장벽(매도 보류)과 조회 중단(해외 주문이 나가야
        # 풀림)이 서로를 기다려 해외 손절·청산이 영구히 막힌다.
        resume = getattr(self._broker, "resume_overseas_reads", None)
        if callable(resume):
            resume()
        try:
            await self.make_fill_poller().poll_once()
        except Exception:
            self._log.exception(
                "order.fill_refresh_failed", lot_id=lot.lot_id if lot is not None else None
            )
            return False
        if not getattr(self._broker, "executions_complete", True):
            # 해외 레그 실패가 빈 목록으로 강등됐다 — 국내 체결은 반영됐지만 해외는 확인 안 됨.
            # 해외 랏의 장벽만 남긴다 (전부 남기면 해외 조회 중단 시 국내 청산까지 영원히 막힘).
            self._log.warning("order.fill_refresh_incomplete")
            overseas = {lot.lot_id for lot in self._tracked_lots() if lot.market.is_overseas}
            self._refresh_before_sell &= overseas
            self._unconfirmed_buy_cancels &= overseas
            # 재시작 장벽은 유지한다: 재시작 전에 취소된(이미 종결된) 해외 매수의 미반영 체결은
            # 보유·걸린 주문 어디에도 흔적이 없어 랏 단위로 가려낼 수 없다. 국내 랏의 매도는
            # 아래 반환값으로 계속 허용되고, flat 판정만 해외 반영 확인까지 보류된다.
            return lot is not None and lot.lot_id not in overseas
        self._refresh_before_sell.clear()
        self._unconfirmed_buy_cancels.clear()
        self._startup_refresh_pending = False
        return True

    async def _cancel_pending(
        self, pending_key: tuple[str, Side], lot: PositionLot, *, reason: str = "replaced_by_exit"
    ) -> bool:
        """미체결 (lot, side) 주문을 취소하고 락을 푼다. 실패 시 False (락 유지).

        메타(_co_map)는 지우지만, 취소 직전에 체결된 분량이 나중에 폴링으로 와도
        ``_on_fill`` 이 DB 의 주문 행으로 메타를 복원해 메모리 랏에 반영한다 (#4)."""
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
        await self._notifier.notify("order.cancelled", ticker=lot.ticker, reason=reason)
        return True

    async def _cancel_open_buy(self, lot: PositionLot, *, reason: str) -> None:
        """청산(EXIT·킬스위치) 전에 같은 랏의 매수 미체결을 취소한다 (#5).

        남겨 두면 청산으로 CLOSED 가 된 뒤 매수 잔량이 체결돼 관리 밖 주식이 생긴다.
        취소가 확인되지 않아도(UNKNOWN 등) 매도는 막지 않는다 — 보유분 청산이 우선이고,
        늦게 온 매수 체결은 ``_on_fill`` 이 랏을 다시 열어 관리한다."""
        key = (lot.lot_id, Side.BUY)
        if key not in self._pending or await self._release_if_terminal(key):
            return
        if not await self._cancel_pending(key, lot, reason=reason):
            self._log.warning("order.exit.buy_cancel_failed", lot_id=lot.lot_id)
            return
        # 취소는 잔량만 없앤다 — 직전 폴링 이후의 부분체결이 아직 메모리에 없을 수 있다. 반영을
        # 확인하기 전엔 flat 이 아니다 (보유 0 으로 보고 긴급중지를 해제·종료하면 뒤늦게 반영된
        # 주식이 청산 없이 남는다). 실패하면 표시가 남아 다음 flat-all 패스에서 재시도한다.
        self._unconfirmed_buy_cancels.add(lot.lot_id)
        await self._refresh_fills(lot)

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
            # await 사이에 다른 코루틴이 새 주문으로 잠금을 바꿨을 수 있다 — 내가 본 주문일
            # 때만 푼다 (새 주문의 잠금을 지우면 중복 주문이 나간다).
            if self._pending.get(pending_key) == cid:
                self._pending.pop(pending_key, None)
            self._co_map.pop(cid, None)
            return True
        return False

    async def _flat_all(self) -> None:
        submitted = False
        # 이미 끝난(거부·만료·UNKNOWN→REJECTED 등) 주문의 잠금을 먼저 정리한다 — 남아 있으면
        # is_flat() 이 영원히 거짓이라 청산 완료를 판정하지 못한다.
        for key in list(self._pending):
            await self._release_if_terminal(key)
        if self._unconfirmed_buy_cancels or self._startup_refresh_pending:
            await self._refresh_fills()  # 매수 취소 뒤·재시작 뒤 체결 반영 (재시도 포함)
        # 슬롯 밖 랏(재시작 전 CLOSED 됐는데 매수가 살아 있는 랏, 슬롯이 점유돼 편입 못 한
        # 재오픈 랏)까지 전부 — 매수 취소와 보유 청산 모두의 대상이다.
        for lot in self._tracked_lots():
            # 진입 대기 중인 매수도 취소 — 긴급중지 뒤에 새 보유가 생기면 안 된다.
            await self._cancel_open_buy(lot, reason="kill_switch")
            if not lot.is_open:
                continue
            price = self._last_price.get(lot.ticker, lot.avg_entry)
            limit_px = round_to_tick(price, Side.SELL) if not lot.market.is_overseas else price
            working = self._co_map.get(self._pending.get((lot.lot_id, Side.SELL), ""))
            if working is not None and working.price == limit_px and working.requested_qty >= lot.qty:
                continue  # 같은 가격의 청산 주문이 이미 걸려 있음 — 매 봉 취소/재주문 방지
            # 걸린 매도(익절 등)나 시세가 바뀐 청산 주문은 취소 후 현재가로 다시 낸다.
            # 급락 중 지정가 청산이 미체결로 남는 것을 막는 재가격(re-price) 경로다.
            ok = await self._submit(
                lot, Side.SELL, lot.qty, price, is_add=False, reason="kill_switch",
                replace_pending=True,
            )
            submitted = submitted or ok
        # flat_all re-runs every bar until all lots are flat; only notify when this pass
        # actually placed an order (otherwise the notifier is spammed once per bar).
        if submitted:
            await self._notifier.notify("kill_switch.flat_all")

    # -- fills (composed: persist + in-memory lot sync) ------------------

    async def _on_fill(self, fill: Fill) -> None:
        await self._om.handle_fill(fill)  # DB: order/fill/position projection + audit
        meta = self._co_map.get(fill.client_order_id)
        if meta is None:
            # 재시작 전에 낸 주문, 또는 취소 직전에 체결된 분량 — 메모리 메타가 없어도 DB 의
            # 주문 행으로 랏을 찾아 반영해야 메모리 랏(전략·손절의 기준)이 실제 보유와 맞는다.
            meta = await self._recover_meta(fill)
            if meta is None:
                return
        lot, side, is_add = meta.lot, meta.side, meta.is_add
        was_closed = lot.state is PositionState.CLOSED
        before = lot.realized_pnl
        lot.apply_fill(side, fill.qty, fill.price, fill.fee, fill.tax, is_add=is_add)
        self._daily_realized += lot.realized_pnl - before  # 당일 한도 계산용 (비용 포함)
        if was_closed and lot.is_open:
            await self._adopt_reopened(lot)
        await self._sync_runtime(lot)  # 진입 체결로 확정된 initial_stop/original_qty 즉시 영속
        meta.filled_qty += fill.qty
        if meta.filled_qty >= meta.requested_qty:
            if self._pending.get((lot.lot_id, side)) == fill.client_order_id:
                self._pending.pop((lot.lot_id, side), None)
            self._co_map.pop(fill.client_order_id, None)

    async def _recover_meta(self, fill: Fill) -> _PendingOrder | None:
        rebuilt: PositionLot | None = None
        async with session_scope(self._sf) as session:
            order = (
                await session.execute(
                    select(Order).where(Order.client_order_id == fill.client_order_id)
                )
            ).scalar_one_or_none()
            if order is None:
                return None  # OrderManager 가 이미 fill.unknown_order 로 경고
            lot = self._lots_by_id.get(order.lot_id)
            if lot is None:
                # 메모리에 없는 랏 — 재시작 전에 CLOSED 라 hydrate 가 싣지 않은 랏에 걸려 있던
                # 매수 잔량이 체결된 경우. handle_fill 이 DB 투영을 이미 재오픈했으므로 그 행으로
                # 랏을 재구성해 곧바로 관리에 편입한다 (다음 재시작까지 방치하면 손절·킬스위치
                # 어느 쪽도 이 주식을 모른다). 행이 이번 체결을 이미 담고 있어 여기서 끝낸다.
                row = await session.get(Position, order.lot_id)
                if row is None or row.qty_filled <= 0 or row.state not in _OPEN_POSITION_STATES:
                    self._log.warning(
                        "fill.lot_not_in_memory", lot_id=order.lot_id, client_order_id=fill.client_order_id
                    )
                    return None
                rebuilt = self._lot_from_row(row)
                self._lots_by_id[rebuilt.lot_id] = rebuilt
                meta = await self._meta_from_order(session, order, rebuilt)
                if meta.filled_qty < meta.requested_qty and order.state not in _TERMINAL_ORDER_STATES:
                    self._co_map[fill.client_order_id] = meta  # 잔량이 아직 살아 있음 — 잠금 유지
                    self._pending[(rebuilt.lot_id, meta.side)] = fill.client_order_id
            else:
                meta = await self._meta_from_order(session, order, lot)
        if rebuilt is not None:
            await self._adopt_reopened(rebuilt)  # 세션 밖에서 (슬롯 정리가 DB 에 쓴다)
            return None
        assert lot is not None
        meta.filled_qty -= fill.qty  # handle_fill 이 이번 체결을 이미 DB 에 기록했다
        if order.state not in _TERMINAL_ORDER_STATES or order.state == OrderState.FILLED.value:
            self._co_map[fill.client_order_id] = meta  # 이어지는 부분체결도 같은 메타로
        self._log.info("fill.meta_recovered", client_order_id=fill.client_order_id, lot_id=lot.lot_id)
        return meta

    @staticmethod
    async def _meta_from_order(session: AsyncSession, order: Order, lot: PositionLot) -> _PendingOrder:
        """DB 주문 행 → 체결 반영용 메타. is_add 는 '이 랏의 첫 매수 주문이 아니면 추가매수'."""
        side = Side(order.side)
        filled = sum(
            (
                await session.execute(select(FillRow.qty).where(FillRow.order_id == order.order_id))
            ).scalars().all(),
            Decimal(0),
        )
        is_add = False
        if side is Side.BUY:
            first_buy = (
                await session.execute(
                    select(Order.client_order_id)
                    .where(Order.lot_id == order.lot_id, Order.side == Side.BUY.value)
                    .order_by(Order.created_at, Order.order_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            is_add = first_buy is not None and first_buy != order.client_order_id
        return _PendingOrder(lot, side, is_add, order.qty, filled_qty=filled, price=order.price)

    async def _adopt_reopened(self, lot: PositionLot) -> None:
        """CLOSED 뒤 매수 체결로 다시 열린 랏을 관리 슬롯에 되돌린다 (#5)."""
        key = self.lot_key(lot.ticker, lot.params.resolution)
        current = self._lots.get(key)
        if current is not None and current is not lot:
            idle = (
                current.state is PositionState.WATCHING
                and current.qty == 0
                and (current.lot_id, Side.BUY) not in self._pending
            )
            if not idle:
                # 슬롯의 새 랏도 보유 중 — 둘 다 관리할 수 없다. 사람이 정리해야 한다.
                self._log.error("fill.orphan_unmanaged", lot_id=lot.lot_id, ticker=lot.ticker)
                await self._notifier.notify(
                    "fill.orphan_unmanaged", ticker=lot.ticker, qty=str(lot.qty), 조치="수동 청산 필요"
                )
                return
            await self._retire_idle_lot(current)
        self._lots[key] = lot
        self._log.warning("fill.orphan_reopened", lot_id=lot.lot_id, ticker=lot.ticker, qty=str(lot.qty))
        await self._notifier.notify(
            "fill.orphan_reopened", ticker=lot.ticker, qty=str(lot.qty), 사유="청산 뒤 매수 잔량 체결"
        )

    async def _retire_idle_lot(self, lot: PositionLot) -> None:
        lot.transition_to(PositionState.CANCELLED)
        async with session_scope(self._sf) as session:
            row = await session.get(Position, lot.lot_id)
            if row is not None:
                row.state = lot.state.value
                row.closed_at = datetime.now(UTC)

    # -- helpers ---------------------------------------------------------

    async def _spawn(self, ticker: str, template: StrategyTemplate) -> PositionLot:
        lot = PositionFactory.create(Signal(ticker=ticker, market=template.market), template)
        self._lots[self.lot_key(ticker, template.resolution)] = lot
        self._lots_by_id[lot.lot_id] = lot
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
        # 체결 전(WATCHING)에는 진입 주문의 손절가·의도 수량을 같은 컬럼에 영속한다 —
        # 체결 전 재시작해도 hydrate() 가 되살려 체결 시 손절가가 적용되게 (#3).
        stop = lot.initial_stop
        original_qty = lot.original_qty
        if lot.state is PositionState.WATCHING:
            stop = lot.pending_stop
            original_qty = lot.pending_original_qty or Decimal(0)
        snapshot = (
            str(stop),
            str(lot.peak_price),
            lot.tp_rungs_taken,
            str(original_qty),
        )
        if self._runtime_synced.get(lot.lot_id) == snapshot:
            return
        async with session_scope(self._sf) as session:
            row = await session.get(Position, lot.lot_id)
            if row is None:
                return
            row.initial_stop = stop if stop is not None else Decimal(0)
            row.peak_price = lot.peak_price
            row.tp_rungs_taken = lot.tp_rungs_taken
            row.original_qty = original_qty
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

    async def _cached_equity(self) -> Decimal | None:
        """평가금 — 최대 EQUITY_TTL_SECONDS 동안 캐시. 실패 시 직전 값을 유지한다.

        조회 실패마다 화면이 '—' 로 깜빡이면 오히려 이상해 보인다. 값이 아주 없을 때만 None."""
        now = time.monotonic()
        if self._equity_cache is not None and now - self._equity_at < EQUITY_TTL_SECONDS:
            return self._equity_cache
        try:
            self._equity_cache = await self._equity()
            self._equity_at = now
        except Exception:
            self._log.warning("equity.refresh_failed")  # 직전 값 유지 (있으면)
        return self._equity_cache

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
        # 슬롯 밖 보유(재오픈 고아 등)도 실제 보유다 — 한도 계산에서 빼면 초과 진입이 난다.
        open_lots = [lot for lot in self._tracked_lots() if lot.is_open]
        exposure: dict[str, Decimal] = {}
        for lot in open_lots:
            price = self._last_price.get(lot.ticker, lot.avg_entry)
            rate = self._fx_rate_checked(lot.currency)
            exposure[lot.ticker] = exposure.get(lot.ticker, Decimal(0)) + lot.qty * price * rate
        # 걸려 있는 매수도 한도에 센다: 라이브 체결은 폴링 뒤에야 반영되므로, 체결분만 세면
        # 같은 분에 신호가 몰릴 때 max_open_positions·종목 노출 한도를 모두 통과한다 (#9).
        pending_lots: set[str] = set()
        for cid in self._pending.values():
            po = self._co_map.get(cid)
            if po is None or po.side is not Side.BUY:
                continue
            remaining = max(Decimal(0), po.requested_qty - po.filled_qty)
            rate = self._fx_rate_checked(po.lot.currency)
            exposure[po.lot.ticker] = exposure.get(po.lot.ticker, Decimal(0)) + remaining * po.price * rate
            if not po.lot.is_open:
                pending_lots.add(po.lot.lot_id)
        self._peak_equity = max(self._peak_equity, equity)
        return RiskSnapshot(
            equity=equity,
            open_positions=len(open_lots) + len(pending_lots),
            daily_pnl=self._daily_realized,  # 당일 실현손익 (자정 리셋)
            ticker_exposure=exposure,
            peak_equity=self._peak_equity,
        )


def _kst_day(ts: datetime) -> _date:
    if ts.tzinfo is None:  # SQLite drops tzinfo (UTC 로 저장됨)
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(_KST).date()
