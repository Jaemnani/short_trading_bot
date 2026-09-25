"""킬스위치 영속·리스크 상태 복원·잔고 캐시·미체결 한도·실전 게이트 (#7 #8 #9 #10 #13, #21)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from short_trading_bot.app.cli import app
from short_trading_bot.app.service import TradingService
from short_trading_bot.domain.enums import Resolution, Side
from short_trading_bot.execution.types import AccountBalance, Fill
from short_trading_bot.infra.config import get_settings
from short_trading_bot.market.types import Bar
from short_trading_bot.risk.control import ControlSwitch
from short_trading_bot.risk.control_file import apply_command, kill_switch_active
from short_trading_bot.risk.limits import RiskLimits
from short_trading_bot.risk.manager import RiskManager
from short_trading_bot.strategy.templates import StrategyTemplate
from tests.test_fill_sync import RestingBroker

_TMPL = StrategyTemplate(strategy_id="trend_long_v1")


def _svc(sf, broker: RestingBroker | None = None) -> TradingService:
    return TradingService(broker or RestingBroker(), sf, RiskManager(RiskLimits()), {})


# --- #7 킬스위치 영속 -------------------------------------------------------------------


def test_stop_is_persisted_and_resume_clears(tmp_path: Path) -> None:
    marker = tmp_path / "kill_switch.active"
    control = ControlSwitch()
    apply_command(control, "stop", kill_switch_path=marker)
    assert control.is_stopped and kill_switch_active(marker)
    apply_command(control, "pause", kill_switch_path=marker)
    assert kill_switch_active(marker)  # pause 는 긴급중지를 풀지 않는다
    apply_command(control, "resume", kill_switch_path=marker)
    assert control.is_running and not kill_switch_active(marker)


async def test_is_flat_requires_no_holdings_and_no_working_orders(sf) -> None:
    svc = _svc(sf)
    lot = await svc._spawn("005930", _TMPL)
    assert svc.is_flat()
    await svc._submit(lot, Side.BUY, Decimal(10), Decimal(100), is_add=False, reason="enter")
    assert not svc.is_flat()  # 걸린 주문 → 아직 종료하면 안 됨
    await svc._on_fill(Fill(svc._pending[(lot.lot_id, Side.BUY)], Decimal(10), Decimal(100)))
    assert not svc.is_flat()  # 보유 중
    await svc._submit(lot, Side.SELL, Decimal(10), Decimal(100), is_add=False, reason="kill_switch")
    await svc._on_fill(Fill(svc._pending[(lot.lot_id, Side.SELL)], Decimal(10), Decimal(100)))
    assert svc.is_flat()


# --- #8 잔고 조회 실패가 손절 평가를 건너뛰게 하지 않음 --------------------------------------


class _BalanceDown(RestingBroker):
    async def get_balance(self) -> AccountBalance:
        raise RuntimeError("KIS HTTP 500 EGW00201")


async def test_balance_failure_does_not_skip_lot_evaluation(sf, monkeypatch) -> None:
    svc = TradingService(_BalanceDown(), sf, RiskManager(RiskLimits()), {"005930": _TMPL})
    calls: list[Decimal] = []

    def fake_evaluate(self, snap, equity, **_kw):  # type: ignore[no-untyped-def]
        calls.append(equity)
        return []

    monkeypatch.setattr("short_trading_bot.domain.position.PositionLot.evaluate", fake_evaluate)
    bar = Bar(
        "005930", Resolution.D1, datetime(2026, 9, 25, tzinfo=UTC),
        Decimal(100), Decimal(101), Decimal(99), Decimal(100), Decimal(1000), Decimal(100000),
    )
    await svc.process(bar)  # 예외 없이
    assert calls == [Decimal(0)]


# --- #9 미체결 매수도 한도에 센다 ------------------------------------------------------------


async def test_risk_snapshot_counts_resting_buys(sf) -> None:
    svc = _svc(sf)
    a = await svc._spawn("005930", _TMPL)
    b = await svc._spawn("000660", _TMPL)
    await svc._submit(a, Side.BUY, Decimal(10), Decimal(70000), is_add=False, reason="enter")
    await svc._submit(b, Side.BUY, Decimal(5), Decimal(20000), is_add=False, reason="enter")
    await svc._on_fill(Fill(svc._pending[(b.lot_id, Side.BUY)], Decimal(2), Decimal(20000)))

    snap = svc._risk_snapshot(Decimal("100000000"))

    assert snap.open_positions == 2  # a: 미체결 대기, b: 부분체결 보유
    assert snap.ticker_exposure["005930"] == Decimal(700000)
    # b 는 체결 2주(평가) + 미체결 3주
    assert snap.ticker_exposure["000660"] == Decimal(2) * Decimal(20000) + Decimal(3) * Decimal(20000)


# --- #10 재시작해도 일일 손실·최고 평가금 유지 ---------------------------------------------


async def test_daily_realized_and_peak_survive_restart(sf) -> None:
    s1 = _svc(sf)
    lot = await s1._spawn("005930", _TMPL)
    await s1._submit(lot, Side.BUY, Decimal(10), Decimal(1000), is_add=False, reason="enter")
    await s1._on_fill(Fill(s1._pending[(lot.lot_id, Side.BUY)], Decimal(10), Decimal(1000)))
    await s1._submit(lot, Side.SELL, Decimal(10), Decimal(900), is_add=False, reason="stop")
    await s1._on_fill(
        Fill(s1._pending[(lot.lot_id, Side.SELL)], Decimal(10), Decimal(900), fee=Decimal(5), tax=Decimal(3))
    )
    assert s1._daily_realized == Decimal(-1008)
    s1._peak_equity = Decimal("12345678")
    await s1._persist_peak()

    s2 = _svc(sf)
    await s2.hydrate()

    assert s2._daily_realized == Decimal(-1008)
    assert s2._peak_equity == Decimal("12345678")


# --- #13 실전 실주문 게이트 -------------------------------------------------------------


@pytest.fixture
def live_env(monkeypatch, tmp_path):
    monkeypatch.setenv("STB_MODE", "LIVE")
    monkeypatch.setenv("STB_KIS__LIVE__APP_KEY", "k")
    monkeypatch.setenv("STB_KIS__LIVE__APP_SECRET", "s")
    monkeypatch.setenv("STB_KIS__LIVE__ACCOUNT_NO", "12345678-01")
    monkeypatch.setenv("STB_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_live_exec_refused_while_dry_run(live_env) -> None:
    result = CliRunner().invoke(app, ["serve", "--config", "watchlist.example.json", "--live-exec"])
    assert result.exit_code == 1
    assert "STB_DRY_RUN=true" in result.output


def test_live_exec_refused_when_preflight_fails(live_env, monkeypatch) -> None:
    monkeypatch.setenv("STB_DRY_RUN", "false")
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["serve", "--config", "watchlist.example.json", "--live-exec"])
    assert result.exit_code == 1
    assert "preflight" in result.output and "api_credentials_secure" in result.output


async def test_peak_not_persisted_for_simulated_paper_broker(sf) -> None:
    """시뮬 현금은 재시작마다 초기화 — 이전 최고 평가금을 복원하면 가짜 낙폭 브레이크가 걸린다."""
    from short_trading_bot.execution.broker.paper import PaperBrokerAdapter

    s1 = TradingService(PaperBrokerAdapter(), sf, RiskManager(RiskLimits()), {})
    s1._peak_equity = Decimal("999999999")
    await s1._persist_peak()
    s2 = TradingService(PaperBrokerAdapter(), sf, RiskManager(RiskLimits()), {})
    await s2.hydrate()
    assert s2._peak_equity == Decimal(0)


async def test_risk_snapshot_counts_slot_collision_holdings(sf) -> None:
    """슬롯 밖 보유(재오픈 고아)도 포지션 수·종목 노출 한도에 들어간다."""
    svc = _svc(sf)
    old = await svc._spawn("035420", _TMPL)
    await svc._submit(old, Side.BUY, Decimal(3), Decimal(10000), is_add=False, reason="enter")
    await svc._on_fill(Fill(svc._pending[(old.lot_id, Side.BUY)], Decimal(3), Decimal(10000)))
    new = await svc._spawn("035420", _TMPL)  # 같은 슬롯을 새 랏이 차지 — old 는 슬롯 밖
    await svc._submit(new, Side.BUY, Decimal(2), Decimal(10000), is_add=False, reason="enter")
    await svc._on_fill(Fill(svc._pending[(new.lot_id, Side.BUY)], Decimal(2), Decimal(10000)))
    svc._last_price["035420"] = Decimal(10000)

    snap = svc._risk_snapshot(Decimal(10**8))
    assert snap.open_positions == 2
    assert snap.ticker_exposure["035420"] == Decimal(50000)


async def test_equity_cache_reuses_cash_but_revalues_holdings(sf) -> None:
    """잔고(현금) 조회만 캐시 — 보유 평가는 매번 새로 계산해 급락이 즉시 평가금에 반영된다."""
    class _Cash(RestingBroker):
        def __init__(self) -> None:
            super().__init__()
            self.balance_calls = 0

        async def get_balance(self) -> AccountBalance:
            from short_trading_bot.domain.enums import Currency

            self.balance_calls += 1
            return AccountBalance(cash={Currency.KRW: Decimal(1_000_000)})

    broker = _Cash()
    svc = _svc(sf, broker)
    lot = await svc._spawn("005930", _TMPL)
    await svc._submit(lot, Side.BUY, Decimal(10), Decimal(10000), is_add=False, reason="enter")
    await svc._on_fill(Fill(svc._pending[(lot.lot_id, Side.BUY)], Decimal(10), Decimal(10000)))
    svc._last_price["005930"] = Decimal(10000)
    assert await svc._cached_equity() == Decimal(1_100_000)
    svc._last_price["005930"] = Decimal(7000)  # 급락
    assert await svc._cached_equity() == Decimal(1_070_000)
    assert broker.balance_calls == 1  # 현금 조회는 TTL 캐시

    svc._lots.pop(svc.lot_key("005930", lot.params.resolution))  # 슬롯 밖 보유도 평가에 포함
    assert await svc._cached_equity() == Decimal(1_070_000)


async def test_fill_adjusts_cached_cash_immediately(sf) -> None:
    """체결 즉시 캐시된 현금을 조정하고, 다음 호출에선 브로커 잔고로 다시 맞춘다.

    - 조회가 실패하면 조정된 추정치로 평가 (매수 직후 매수 대금만큼 부풀지 않게)
    - 조회가 되면 브로커 값 그대로 (체결 후·폴링 전에 조회된 잔고에 이중 반영 금지)"""
    from short_trading_bot.domain.enums import Currency

    class _Cash(RestingBroker):
        def __init__(self) -> None:
            super().__init__()
            self.cash = Decimal(1_000_000)
            self.down = False

        async def get_balance(self) -> AccountBalance:
            if self.down:
                raise RuntimeError("KIS 500")
            return AccountBalance(cash={Currency.KRW: self.cash})

    broker = _Cash()
    svc = _svc(sf, broker)
    assert await svc._cached_equity() == Decimal(1_000_000)
    lot = await svc._spawn("005930", _TMPL)
    await svc._submit(lot, Side.BUY, Decimal(10), Decimal(10000), is_add=False, reason="enter")
    await svc._on_fill(
        Fill(svc._pending[(lot.lot_id, Side.BUY)], Decimal(10), Decimal(10000), fee=Decimal(15))
    )
    svc._last_price["005930"] = Decimal(10000)

    broker.down = True  # 재조회 실패 → 조정된 추정 현금 사용
    assert await svc._cached_equity() == Decimal(1_000_000) - Decimal(15)

    broker.down = False
    broker.cash = Decimal(1_000_000) - Decimal(100_015)  # 브로커는 이미 체결 반영
    assert await svc._cached_equity() == Decimal(1_000_000) - Decimal(15)  # 이중 차감 없음


async def test_prior_day_fill_excluded_from_today_realized(sf) -> None:
    """전일 체결 복구분(그날 마감 시각)은 오늘 일일 실현손익 누계에 들어가지 않는다."""
    from datetime import timedelta, timezone

    svc = _svc(sf)
    lot = await svc._spawn("005930", _TMPL)
    await svc._submit(lot, Side.BUY, Decimal(10), Decimal(1000), is_add=False, reason="enter")
    await svc._on_fill(Fill(svc._pending[(lot.lot_id, Side.BUY)], Decimal(10), Decimal(1000)))
    kst = timezone(timedelta(hours=9))
    svc._daily_date = datetime.now(kst).date()  # 엔진의 '오늘' = KST 거래일
    svc._daily_realized = Decimal(0)
    await svc._submit(lot, Side.SELL, Decimal(10), Decimal(900), is_add=False, reason="stop")
    yesterday_close = datetime.now(kst) - timedelta(days=1)
    await svc._on_fill(
        Fill(svc._pending[(lot.lot_id, Side.SELL)], Decimal(10), Decimal(900), ts=yesterday_close)
    )
    assert lot.realized_pnl == Decimal(-1000)  # 랏 손익은 반영
    assert svc._daily_realized == Decimal(0)  # 오늘 한도 누계엔 안 들어감
