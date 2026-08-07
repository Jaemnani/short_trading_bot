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


async def test_routing_overseas_read_failure_isolated() -> None:
    """해외 read 실패가 국내 체결 배달을 볼모로 잡지 않는다 (모의 도메인 inquire-ccnl 500 실측).

    연속 3회 실패 후엔 해외 폴링을 쉬고, 해외 주문이 다시 나가면 재개한다."""

    class _BrokenOverseas(PaperBrokerAdapter):
        calls = 0

        async def get_executions(self):  # type: ignore[override]
            self.calls += 1
            raise RuntimeError("VTS 500")

    cfg = PaperConfig(enforce_funds=False)
    domestic, overseas = PaperBrokerAdapter(cfg), _BrokenOverseas(cfg)
    router = RoutingBrokerAdapter(domestic, overseas)
    router.fill_handler = functools.partial(_collect, [])
    await router.submit_order(_order(Market.KRX, "005930"))  # 국내 체결 1건 생성

    for _ in range(5):  # 해외가 계속 500이어도 국내 체결은 매번 배달
        execs = await router.get_executions()
        assert [e.ticker for e in execs] == ["005930"]
    assert overseas.calls == 3  # 연속 3회 실패 후 해외 폴링 중단

    await router.submit_order(_order(Market.NASD, "AAPL"))  # 해외 주문 → 폴링 재개
    await router.get_executions()
    assert overseas.calls == 4


async def test_routing_overseas_balance_failure_degrades_to_domestic() -> None:
    """해외 잔고 500이 equity/reconcile를 죽이면 안 된다 (2026-08-03 엔진 나흘 다운 원인).

    실패 시 국내 잔고만으로 강등 — 과소 equity = 보수적 사이징이라 안전한 방향."""

    class _BrokenBalance(PaperBrokerAdapter):
        async def get_balance(self):  # type: ignore[override]
            raise RuntimeError("VTS inquire-balance 500")

    cfg = PaperConfig(enforce_funds=False)
    domestic = PaperBrokerAdapter(cfg)
    router = RoutingBrokerAdapter(domestic, _BrokenBalance(cfg))
    router.fill_handler = functools.partial(_collect, [])
    await router.submit_order(_order(Market.KRX, "005930"))

    merged = await router.get_balance()  # 예외 없이 국내 잔고만
    assert [p.ticker for p in merged.positions] == ["005930"]
    assert merged.cash  # 국내 현금은 살아있음


# --- KisWebSocketFeed parsing ---

def _frame(ticker: str, hhmmss: str, price: str, volume: str) -> str:
    # CNTG_VOL (per-trade volume) is at index 12; index 13 is ACML_VOL (accumulated)
    fields = [ticker, hhmmss, price, *(["0"] * 9), volume, "0"]
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


async def test_ws_feed_shared_builder_survives_reconnect() -> None:
    """재접속(새 피드 인스턴스)에도 공유 BarBuilder가 만들던 봉을 보존한다."""
    from short_trading_bot.market.bar_builder import BarBuilder

    shared = BarBuilder(Resolution.M1)
    feed1 = KisWebSocketFeed(
        "a", ["005930"], Resolution.M1, connect=lambda: _FakeWs([_frame("005930", "090000", "100", "5")]),
        session_date=date(2026, 1, 2), bar_builder=shared, flush_on_close=False,
    )
    assert [b async for b in feed1.stream()] == []  # 부분 봉 방출 없음 (유실도 없음)

    feed2 = KisWebSocketFeed(  # 재접속: 같은 분(09:00) 틱 + 다음 분 틱
        "a", ["005930"], Resolution.M1,
        connect=lambda: _FakeWs([
            _frame("005930", "090030", "105", "5"),
            _frame("005930", "090100", "101", "1"),
        ]),
        session_date=date(2026, 1, 2), bar_builder=shared, flush_on_close=False,
    )
    bars = [b async for b in feed2.stream()]
    assert len(bars) == 1
    # 09:00 봉이 단절 전(100/5) + 후(105/5) 틱을 모두 포함
    assert bars[0].open == Decimal("100") and bars[0].high == Decimal("105")
    assert bars[0].volume == Decimal("10")


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


def test_build_broker_domestic_only_by_default(monkeypatch) -> None:
    """overseas_enabled 기본 False — 해외 어댑터를 만들지 않아 해외 API 호출이 0이다."""
    from short_trading_bot.infra.config import KisEnvCreds, KisSettings, Settings

    s = Settings(
        _env_file=None,
        kis=KisSettings(paper=KisEnvCreds(app_key="k", app_secret="s", account_no="123-01")),
    )
    broker = build_broker(s)
    assert isinstance(broker, RoutingBrokerAdapter)
    assert broker._overseas is None

    s2 = s.model_copy(update={"overseas_enabled": True})
    assert build_broker(s2)._overseas is not None
