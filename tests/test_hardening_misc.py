"""잡다한 보안·견고성 회귀 테스트 (#12 #18 #19 #22 #23)."""

from __future__ import annotations

import io
import os
import stat
import zipfile
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from short_trading_bot.domain.enums import Market, Resolution, Side
from short_trading_bot.execution.broker.paper import PaperBrokerAdapter, PaperConfig
from short_trading_bot.execution.types import Fill, OrderRequest
from short_trading_bot.infra.notifier.base import CompositeNotifier, Notifier, redact_urls
from short_trading_bot.infra.notifier.discord import DiscordNotifier
from short_trading_bot.infra.notifier.kakao import KakaoToken
from short_trading_bot.market.bar_builder import BarBuilder
from short_trading_bot.market.kis_ws_feed import KisWebSocketFeed
from short_trading_bot.news.risk import MAX_UNZIPPED_BYTES, DartRiskChecker

_HOOK = "https://discord.com/api/webhooks/123456/SECRET-TOKEN-abc"


# --- #12 웹후크 URL 이 로그로 새지 않음 --------------------------------------------------


async def test_discord_http_error_message_has_no_webhook_url(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "rate limited"})

    real_client = httpx.AsyncClient

    def fake_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    with pytest.raises(RuntimeError) as exc:
        await DiscordNotifier(_HOOK).notify("x")
    assert "SECRET-TOKEN" not in str(exc.value) and "429" in str(exc.value)


async def test_composite_notifier_redacts_urls_in_failure_log(monkeypatch) -> None:
    class Boom(Notifier):
        async def notify(self, event: str, **fields: Any) -> None:
            raise RuntimeError(f"Client error '404 Not Found' for url '{_HOOK}'")

    logged: list[dict[str, Any]] = []
    composite = CompositeNotifier(Boom())
    monkeypatch.setattr(composite._log, "warning", lambda event, **kw: logged.append(kw))
    await composite.notify("x")
    assert logged and "SECRET-TOKEN" not in logged[0]["error"]
    assert redact_urls(f"see {_HOOK} now") == "see <url> now"


# --- #19 카카오 토큰 파일 권한 ------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX 권한")
def test_kakao_token_saved_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "kakao_token.json"
    stale_tmp = path.with_suffix(".tmp")
    stale_tmp.write_text("{}")
    stale_tmp.chmod(0o644)  # 예전 실행이 남긴 넓은 권한의 tmp
    KakaoToken("a", "r", 1.0).save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert KakaoToken.load(path) == KakaoToken("a", "r", 1.0)


# --- #18 WS 틱 검증 ---------------------------------------------------------------------


def _frame(*records: tuple[str, str, str, str]) -> str:
    fields: list[str] = []
    for ticker, hhmmss, price, volume in records:
        fields += [ticker, hhmmss, price, *(["0"] * 9), volume, "0"]
    return f"0|H0STCNT0|{len(records):03d}|" + "^".join(fields)


def test_parse_ticks_drops_bad_records_keeps_good_ones() -> None:
    raw = _frame(
        ("005930", "093000", "70000", "1"),
        ("005930", "09xx00", "70000", "1"),  # 깨진 시각
        ("005930", "093001", "NaN", "1"),
        ("005930", "093002", "-5", "1"),
        ("005930", "093003", "0", "1"),
        ("005930", "093004", "Infinity", "1"),
        ("005930", "093005", "abc", "1"),
        ("005930", "093006", "70100", "2"),
    )
    ticks = KisWebSocketFeed.parse_ticks(raw, date(2026, 9, 25))
    assert [t.price for t in ticks] == [Decimal(70000), Decimal(70100)]


class _FakeWs:
    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    def __aiter__(self) -> _FakeWs:
        return self

    async def __anext__(self) -> str:
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


async def test_out_of_order_tick_does_not_kill_stream() -> None:
    frames = [
        _frame(("005930", "093000", "100", "1")),
        _frame(("005930", "093100", "101", "1")),
        _frame(("005930", "093005", "999", "1")),  # 역순 — 버려야 한다
        _frame(("005930", "093200", "102", "1")),
    ]
    ws = _FakeWs(frames)

    @asynccontextmanager
    async def connect() -> Any:
        yield ws

    feed = KisWebSocketFeed(
        "k", ["005930"], Resolution.M1, connect=connect, session_date=date(2026, 9, 25),
        bar_builder=BarBuilder(Resolution.M1), flush_on_close=True,
    )
    bars = [bar async for bar in feed.stream()]
    assert [b.close for b in bars] == [Decimal(100), Decimal(101), Decimal(102)]
    assert all(b.high < 999 for b in bars)


# --- #23 zip bomb 상한 -----------------------------------------------------------------


def test_corp_code_zip_size_capped(monkeypatch) -> None:
    import short_trading_bot.news.risk as risk

    xml = b"<result>" + b"<list><stock_code>005930</stock_code><corp_code>00126380</corp_code></list>" + b"</result>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("CORPCODE.xml", xml + b" " * 4096)
    assert DartRiskChecker.parse_corp_codes(buf.getvalue()) == {"005930": "00126380"}

    monkeypatch.setattr(risk, "MAX_UNZIPPED_BYTES", 1024)
    with pytest.raises(ValueError):
        DartRiskChecker.parse_corp_codes(buf.getvalue())
    assert MAX_UNZIPPED_BYTES > 1024


# --- #22 페이퍼 대기 지정가 (옵트인) -------------------------------------------------------


def _limit(side: Side, price: int, cid: str = "c1") -> OrderRequest:
    return OrderRequest(
        client_order_id=cid, lot_id="l", ticker="005930", market=Market.KRX,
        side=side, qty=Decimal(10), price=Decimal(price), ord_dvsn="00",
    )


async def test_paper_default_still_fills_limits_immediately() -> None:
    broker = PaperBrokerAdapter(PaperConfig())
    fills: list[Fill] = []

    async def on_fill(fill: Fill) -> None:
        fills.append(fill)

    broker.fill_handler = on_fill
    await broker.on_market_price("005930", Decimal(70000))
    await broker.submit_order(_limit(Side.BUY, 60000))  # 시세보다 낮아도 즉시 체결 (기존 동작)
    assert len(fills) == 1


async def test_paper_resting_limits_wait_for_cross_and_can_be_cancelled() -> None:
    broker = PaperBrokerAdapter(PaperConfig(resting_limits=True))
    fills: list[Fill] = []

    async def on_fill(fill: Fill) -> None:
        fills.append(fill)

    broker.fill_handler = on_fill
    await broker.on_market_price("005930", Decimal(70000))
    ack = await broker.submit_order(_limit(Side.BUY, 69000))
    assert ack.accepted and fills == [] and len(await broker.get_open_orders()) == 1
    await broker.on_market_price("005930", Decimal(69500))
    assert fills == []
    await broker.on_market_price("005930", Decimal(68900))  # 교차 → 지정가 체결
    assert len(fills) == 1 and fills[0].price == Decimal(69000)

    ack2 = await broker.submit_order(_limit(Side.SELL, 75000, cid="c2"))
    await broker.cancel_order(_limit(Side.SELL, 75000, cid="c2"), ack2.broker_order_no)
    await broker.on_market_price("005930", Decimal(76000))
    assert len(fills) == 1 and await broker.get_open_orders() == []

    marketable = await broker.submit_order(_limit(Side.SELL, 70000, cid="c3"))  # 이미 교차
    assert marketable.accepted and len(fills) == 2


async def test_paper_resting_buys_reserve_cash() -> None:
    """대기 매수는 대금을 예약 — 같은 현금으로 여러 매수가 받아져 함께 체결되면 현금이 음수가 된다."""
    broker = PaperBrokerAdapter(PaperConfig(initial_cash=Decimal(1_000_000), resting_limits=True))
    await broker.on_market_price("005930", Decimal(70000))
    first = await broker.submit_order(_limit(Side.BUY, 69000, cid="a"))  # 10주 = 69만
    second = await broker.submit_order(_limit(Side.BUY, 69000, cid="b"))  # 예약 후 잔여 31만 → 거부
    assert first.accepted and not second.accepted and second.reject_reason == "insufficient_funds"
    await broker.cancel_order(_limit(Side.BUY, 69000, cid="a"), first.broker_order_no)
    third = await broker.submit_order(_limit(Side.BUY, 69000, cid="c"))  # 취소로 예약 해제
    assert third.accepted
    await broker.on_market_price("005930", Decimal(68000))
    assert broker.cash() >= 0
