"""평가금 캐시 — KIS 호출량 감축의 근거.

2026-08-10: 상태 스냅샷이 5초마다 잔고 REST 를 호출해 24시간 ~17,000회. KIS 초당 한도를
갉아먹어 장중 체결 조회(inquire-daily-ccld)가 356회 실패했다. 평가금은 보유 랏 시가평가가
대부분이라 30초 캐시로 충분하다.
"""

from decimal import Decimal

from short_trading_bot.app import service as service_mod
from short_trading_bot.app.service import TradingService
from short_trading_bot.execution.broker.paper import PaperBrokerAdapter, PaperConfig
from short_trading_bot.risk.limits import RiskLimits
from short_trading_bot.risk.manager import RiskManager


class CountingBroker(PaperBrokerAdapter):
    def __init__(self) -> None:
        super().__init__(PaperConfig(initial_cash=Decimal("1000000")))
        self.balance_calls = 0
        self.fail = False

    async def get_balance(self):  # type: ignore[no-untyped-def]
        self.balance_calls += 1
        if self.fail:
            raise RuntimeError("KIS 500 흉내")
        return await super().get_balance()


def _svc(sf) -> tuple[TradingService, CountingBroker]:
    broker = CountingBroker()
    return TradingService(broker, sf, RiskManager(RiskLimits()), {}), broker


async def test_repeated_snapshots_hit_broker_once(sf, monkeypatch) -> None:
    svc, broker = _svc(sf)
    for _ in range(5):
        await svc.status_snapshot()
    assert broker.balance_calls == 1  # 5회 스냅샷 → REST 1회


async def test_cache_expires_after_ttl(sf, monkeypatch) -> None:
    svc, broker = _svc(sf)
    clock = {"t": 1000.0}
    monkeypatch.setattr(service_mod.time, "monotonic", lambda: clock["t"])
    await svc.status_snapshot()
    clock["t"] += service_mod.EQUITY_TTL_SECONDS + 1
    await svc.status_snapshot()
    assert broker.balance_calls == 2


async def test_failure_keeps_last_value(sf) -> None:
    """조회 실패 시 화면이 '—' 로 깜빡이지 않도록 직전 값을 유지한다."""
    svc, broker = _svc(sf)
    first = await svc.status_snapshot()
    assert first["equity"] is not None
    svc._equity_at = 0.0  # TTL 만료 강제
    broker.fail = True
    second = await svc.status_snapshot()
    assert second["equity"] == first["equity"]  # 실패해도 직전 값


async def test_no_value_yet_reports_none(sf) -> None:
    svc, broker = _svc(sf)
    broker.fail = True
    snap = await svc.status_snapshot()
    assert snap["equity"] is None  # 한 번도 못 받았으면 정직하게 None
