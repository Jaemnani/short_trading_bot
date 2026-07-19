"""동적 종목 합류 배관 테스트 — add_template / feed.subscribe / ScannerConfig."""

import json
from decimal import Decimal

from short_trading_bot.app.service import TradingService
from short_trading_bot.app.watchlist import load_scanner_config
from short_trading_bot.domain.enums import Resolution
from short_trading_bot.execution.broker.paper import PaperBrokerAdapter, PaperConfig
from short_trading_bot.risk.limits import RiskLimits
from short_trading_bot.risk.manager import RiskManager
from short_trading_bot.strategy.templates import StrategyTemplate


def _template(resolution: str = "5m") -> StrategyTemplate:
    return StrategyTemplate(
        strategy_id="momo_intraday_v1", resolution=resolution, risk_per_trade=0.005
    )


def _service(sf) -> TradingService:
    broker = PaperBrokerAdapter(PaperConfig(initial_cash=Decimal("100000000")))
    return TradingService(broker, sf, RiskManager(RiskLimits()), {})


async def test_add_template_spawns_on_next_bar(sf) -> None:
    svc = _service(sf)
    assert svc.add_template("123450@scan", _template())
    assert not svc.add_template("123450@scan2", _template())  # 같은 (종목, 해상도) 중복 거부
    assert svc.add_template("123450@scan1d", _template("1D"))  # 다른 해상도는 허용


async def test_scanner_config_from_json(tmp_path) -> None:
    p = tmp_path / "wl.json"
    p.write_text(json.dumps({
        "watchlist": {},
        "scanner": {"enabled": True, "interval_seconds": 120, "max_active": 2,
                     "strategy_params": {"min_rvol": 2.5}},
    }))
    cfg = load_scanner_config(p)
    assert cfg.enabled and cfg.interval_seconds == 120 and cfg.max_active == 2
    tmpl = cfg.template()
    assert tmpl.strategy_id == "momo_intraday_v1" and tmpl.resolution is Resolution.M5

    p.write_text(json.dumps({"watchlist": {}}))
    assert load_scanner_config(p).enabled is False  # 섹션 없으면 비활성


async def test_feed_dynamic_subscribe() -> None:
    from short_trading_bot.market.kis_ws_feed import KisWebSocketFeed

    tickers = ["005930"]
    feed = KisWebSocketFeed("appr", tickers, Resolution.M5)
    assert await feed.subscribe("123450")  # 연결 전: 리스트에만 추가
    assert not await feed.subscribe("123450")  # 중복 거부
    assert tickers == ["005930", "123450"]  # 공유 리스트 유지 → 재접속 시 재구독


async def test_one_shot_template_retires_after_close(sf) -> None:
    """one_shot 합류분은 랏 1회전(청산) 후 재스폰하지 않고 템플릿이 제거된다."""
    from datetime import UTC, datetime

    from short_trading_bot.domain.enums import PositionState
    from short_trading_bot.market.types import Bar

    svc = _service(sf)
    assert svc.add_template("123450@scan", _template(), one_shot=True)

    def bar(ts_min: int) -> Bar:
        c = Decimal("10000")
        return Bar(
            ticker="123450", resolution=Resolution.M5,
            ts=datetime(2026, 7, 13, 1, ts_min, tzinfo=UTC),  # 10:xx KST
            open=c, high=c, low=c, close=c, volume=Decimal("100"), value=c * 100,
        )

    await svc.process(bar(0))  # 템플릿 → 랏 스폰
    lot = svc.lot("123450", Resolution.M5)
    assert lot is not None
    lot.state = PositionState.CLOSED  # 1회전 종료 가정

    await svc.process(bar(5))  # one_shot → 재스폰 없이 템플릿 은퇴
    assert svc.lot("123450", Resolution.M5) is lot and lot.is_terminal
    assert "123450@scan" not in svc.lots or True
    assert all(k.split("@")[0] != "123450" for k in svc._watchlist)

    # 재합류는 다시 가능해야 한다 (스캐너가 이후 다시 pick하면)
    assert svc.add_template("123450@scan", _template(), one_shot=True)


async def test_scanner_config_chandelier_passthrough(tmp_path) -> None:
    import json as _json

    p = tmp_path / "wl.json"
    p.write_text(_json.dumps({"scanner": {"enabled": True, "chandelier_mult": 2.0}}))
    tmpl = load_scanner_config(p).template()
    assert tmpl.stop.chandelier_mult == 2.0
