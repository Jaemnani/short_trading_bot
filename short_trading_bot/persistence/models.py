"""SQLAlchemy 2.x (async) ORM models.

The ``audit_log`` is the append-only source of truth; ``positions``/``trades`` are
projections rebuildable from it. Monetary and quantity fields use ``Numeric`` (Decimal)
for exact P&L accounting. Enum-typed domain values are stored as their string ``value``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Reusable column type aliases.
_Money = Numeric(24, 8)  # Decimal money/quantity (supports fractional overseas shares)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Position(Base):
    """A PositionLot — one dynamically-created, self-contained "stock object" per buy."""

    __tablename__ = "positions"

    lot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    market: Mapped[str] = mapped_column(String(8), default="KRX")
    currency: Mapped[str] = mapped_column(String(3), default="KRW")
    side: Mapped[str] = mapped_column(String(4), default="BUY")  # long-only -> BUY
    state: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64))
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # frozen PositionParams
    resolution: Mapped[str] = mapped_column(String(8), default="1D")

    qty_target: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    qty_filled: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    avg_entry_price: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    realized_pnl: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    unrealized_pnl: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))

    # 런타임 스톱 상태 — 재시작 시 hydrate()가 복원한다. 없으면 복원된 랏이
    # 초기 손절/트레일링/TP 사다리 진행 상황을 잃고 지표 기반으로만 관리된다.
    initial_stop: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    peak_price: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    tp_rungs_taken: Mapped[int] = mapped_column(Integer, default=0)
    original_qty: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))

    parent_signal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.campaign_id"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    orders: Mapped[list[Order]] = relationship(back_populates="position")


class Order(Base):
    """Broker order with idempotency key. client_order_id is persisted BEFORE sending."""

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("positions.lot_id"), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    broker_order_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tr_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ord_dvsn: Mapped[str | None] = mapped_column(String(8), nullable=True)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[Decimal] = mapped_column(_Money)
    price: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))  # 0 = market
    state: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    position: Mapped[Position] = relationship(back_populates="orders")


class Fill(Base):
    __tablename__ = "fills"

    fill_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), index=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("positions.lot_id"), index=True)
    qty: Mapped[Decimal] = mapped_column(_Money)
    price: Mapped[Decimal] = mapped_column(_Money)
    fee: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    tax: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3), default="KRW")
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    source: Mapped[str] = mapped_column(String(16), default="ws")  # ws | reconcile | paper


class Signal(Base):
    """Every generated signal is stored even if not acted on (for later analysis)."""

    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    regime_pass: Mapped[bool] = mapped_column(Boolean, default=False)
    trigger_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confirm_flags_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    indicator_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    news_ewma: Mapped[float | None] = mapped_column(nullable=True)
    acted: Mapped[bool] = mapped_column(Boolean, default=False)
    lot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Trade(Base):
    """A closed round-trip with realized P&L (local currency + KRW via fx_rate)."""

    __tablename__ = "trades"

    trade_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("positions.lot_id"), index=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    market: Mapped[str] = mapped_column(String(8), default="KRX")
    currency: Mapped[str] = mapped_column(String(3), default="KRW")
    entry_price: Mapped[Decimal] = mapped_column(_Money)
    exit_price: Mapped[Decimal] = mapped_column(_Money)
    qty: Mapped[Decimal] = mapped_column(_Money)
    gross_pnl: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    fees: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    tax: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    net_pnl: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    fx_rate: Mapped[Decimal] = mapped_column(_Money, default=Decimal(1))  # 정산환율 to KRW
    r_multiple: Mapped[float | None] = mapped_column(nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Campaign(Base):
    """Period-bounded, capital-isolated trading session (start budget -> forced full liquidation)."""

    __tablename__ = "campaigns"

    campaign_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(16), default="PAPER")
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    initial_budget: Mapped[Decimal] = mapped_column(_Money)
    currency: Mapped[str] = mapped_column(String(3), default="KRW")
    status: Mapped[str] = mapped_column(String(16), default="SCHEDULED", index=True)
    allocation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CampaignReport(Base):
    """Settlement: net_pnl == end_cash - initial_budget (fees/tax included)."""

    __tablename__ = "campaign_reports"

    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.campaign_id"), primary_key=True
    )
    realized_pnl: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    return_pct: Mapped[float | None] = mapped_column(nullable=True)
    end_cash: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    total_fees: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    total_tax: Mapped[Decimal] = mapped_column(_Money, default=Decimal(0))
    per_lot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NewsItem(Base):
    """Official-source news/disclosure metadata + derived sentiment. Article body is NOT stored (ToS)."""

    __tablename__ = "news_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # rcept_no or url hash
    source: Mapped[str] = mapped_column(String(16))  # DART | RSS
    ticker: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pblntf_ty: Mapped[str | None] = mapped_column(String(4), nullable=True)
    sentiment_score: Mapped[float | None] = mapped_column(nullable=True)  # [-1, 1]
    is_high_impact: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SentimentState(Base):
    __tablename__ = "sentiment_state"

    ticker: Mapped[str] = mapped_column(String(20), primary_key=True)
    ewma_score: Mapped[float] = mapped_column(default=0.0)
    half_life_hours: Mapped[float] = mapped_column(default=4.0)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditLog(Base):
    """Append-only event-sourced truth. positions/trades are rebuildable projections."""

    __tablename__ = "audit_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    lot_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ConfigVersion(Base):
    """Every config change versioned for reproducibility."""

    __tablename__ = "config_versions"

    version_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    mode: Mapped[str] = mapped_column(String(16))
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    config_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
