from __future__ import annotations

import functools
from datetime import date
from decimal import Decimal

import pytest

from short_trading_bot.app.engine import build_broker, build_trading_service, is_ready, preflight
from short_trading_bot.domain.enums import Market, Resolution, Side
from short_trading_bot.execution.broker.paper import PaperBrokerAdapter, PaperConfig
from short_trading_bot.execution.broker.routing import RoutingBrokerAdapter
from short_trading_bot.execution.types import Fill, OrderRequest
from short_trading_bot.infra.config import Settings
from short_trading_bot.market.kis_ws_feed import KisWebSocketFeed
from short_trading_bot.risk.limits import RiskLimits

# --- RoutingBrokerAdapter ---

async def _collect(store: list[Fill], f: Fill) -> None:
    store.append(f)


def _order(market: Market, ticker: str) -> OrderRequest:
    return OrderRequest(
        client_order_id=f"c-{ticker}", lot_id="l", ticker=ticker, market=market,
        side=Side.BUY, qty=Decimal("10"), price=Decimal("100"), ord_dvsn="00",
    )


async def test_routing_dispatches_by_market_and_merges() -> None:
    cfg = PaperConfig(enforce_funds=False)  # testing routing/merge, not buying power
    domestic, overseas = PaperBrokerAdapter(cfg), PaperBrokerAdapter(cfg)
    router = RoutingBrokerAdapter(domestic, overseas)
    fills: list[Fill] = []
    router.fill_handler = functools.partial(_collect, fills)
    assert domestic.fill_handler is not None and overseas.fill_handler is not None  # propagated

    await router.submit_order(_order(Market.KRX, "005930"))
    await router.submit_order(_order(Market.NASD, "AAPL"))

    dom_bal = await domestic.get_balance()
    ovs_bal = await overseas.get_balance()
    assert [p.ticker for p in dom_bal.positions] == ["005930"]
    assert [p.ticker for p in ovs_bal.positions] == ["AAPL"]
    merged = await router.get_balance()
    assert {p.ticker for p in merged.positions} == {"005930", "AAPL"}
    assert len(fills) == 2


async def test_routing_without_overseas_raises() -> None:
    router = RoutingBrokerAdapter(PaperBrokerAdapter(PaperConfig()))
    with pytest.raises(ValueError):
        await router.submit_order(_order(Market.NASD, "AAPL"))


# --- KisWebSocketFeed parsing ---

def _frame(ticker: str, hhmmss: str, price: str, volume: str) -> str:
    fields = [ticker, hhmmss, price, *(["0"] * 10), volume]  # index 13 = volume
    return "0|H0STCNT0|001|" + "^".join(fields)


def test_parse_ticks() -> None:
    ticks = KisWebSocketFeed.parse_ticks(_frame("005930", "093045", "70000", "12"), date(2026, 1, 2))
    assert len(ticks) == 1
    assert ticks[0].ticker == "005930"
    assert ticks[0].price == Decimal("70000")
    assert ticks[0].volume == Decimal("12")
    assert ticks[0].ts.hour == 9 and ticks[0].ts.minute == 30


def test_parse_ticks_ignores_control_and_other_tr() -> None:
    assert KisWebSocketFeed.parse_ticks('{"header":{"tr_id":"PINGPONG"}}', date(2026, 1, 2)) == []
    assert KisWebSocketFeed.parse_ticks("0|H0STASP0|001|005930^x", date(2026, 1, 2)) == []


class _FakeWs:
    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self.sent: list[str] = []

    async def __aenter__(self) -> _FakeWs:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def __aiter__(self):
        for frame in self._frames:
            yield frame


async def test_ws_feed_streams_bars() -> None:
    frames = [
        _frame("005930", "093000", "100", "5"),
        _frame("005930", "093030", "105", "5"),  # same 1m bucket
        _frame("005930", "093100", "101", "1"),  # next minute -> completes 09:30 bar
    ]
    feed = KisWebSocketFeed(
        "approval-x", ["005930"], Resolution.M1,
        connect=lambda: _FakeWs(frames), session_date=date(2026, 1, 2),
    )
    bars = [bar async for bar in feed.stream()]
    assert len(bars) == 2  # 09:30 (completed) + 09:31 (flushed on disconnect)
    first = bars[0]
    assert first.open == Decimal("100") and first.high == Decimal("105") and first.close == Decimal("105")
    assert first.volume == Decimal("10") and first.ts.minute == 30


# --- engine builder + preflight ---

def test_build_broker_paper_fallback_without_keys() -> None:
    assert build_broker(Settings(_env_file=None)).name == "paper"


def test_build_broker_routing_with_keys(monkeypatch) -> None:
    monkeypatch.setenv("STB_KIS__PAPER__APP_KEY", "k")
    monkeypatch.setenv("STB_KIS__PAPER__APP_SECRET", "s")
    monkeypatch.setenv("STB_KIS__PAPER__ACCOUNT_NO", "12345678-01")
    assert build_broker(Settings(_env_file=None)).name == "routing"


def test_build_trading_service_paper() -> None:
    svc = build_trading_service(Settings(_env_file=None), {})
    assert svc.lots == {}


def test_preflight_not_ready_without_keys() -> None:
    checks = preflight(Settings(_env_file=None))
    assert not is_ready(checks)
    assert any(c.name == "kis_credentials" and not c.ok for c in checks)


def test_preflight_ready_with_keys(monkeypatch) -> None:
    monkeypatch.setenv("STB_KIS__PAPER__APP_KEY", "k")
    monkeypatch.setenv("STB_KIS__PAPER__APP_SECRET", "s")
    monkeypatch.setenv("STB_KIS__PAPER__ACCOUNT_NO", "12345678-01")
    checks = preflight(Settings(_env_file=None), limits=RiskLimits(daily_loss_limit=Decimal("500000")))
    assert is_ready(checks)
