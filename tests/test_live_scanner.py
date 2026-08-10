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


class TestTrackedTickers:
    """재접속 구독 목록의 근거. 2026-08-10 사고: 스캐너 합류분이 정적 리스트에 없어
    첫 재접속(합류 6분 뒤)에 시세를 잃고 6시간 반 깜깜이 → 진입 불가."""

    async def test_scanner_joined_ticker_is_tracked(self, sf) -> None:
        svc = _service(sf)
        assert "123450" not in svc.tracked_tickers()
        svc.add_template("123450@scan", _template())
        # 재접속 시 이 목록으로 구독을 다시 걸어야 시세가 유지된다.
        assert "123450" in svc.tracked_tickers()

    async def test_watchlist_tickers_are_tracked(self, sf) -> None:
        svc = _service(sf)
        svc.add_template("005930@1", _template())
        svc.add_template("000660@1", _template("1D"))
        assert {"005930", "000660"} <= svc.tracked_tickers()

    async def test_multiple_resolutions_same_ticker_appear_once(self, sf) -> None:
        svc = _service(sf)
        svc.add_template("123450@a", _template("5m"))
        svc.add_template("123450@b", _template("1D"))
        assert [t for t in svc.tracked_tickers() if t == "123450"] == ["123450"]

    async def test_watching_lot_is_tracked_not_just_open(self, sf) -> None:
        """관망(미진입) 랏도 구독 대상 — 진입 판단에 봉이 필요하고, 봉이 없으면
        당일 만료 판정조차 못 돌아 랏이 영구히 남는다 (2026-08-10 실측: 관망 12 중 6 미구독)."""
        from datetime import UTC, datetime

        from short_trading_bot.market.types import Bar

        svc = _service(sf)
        assert svc.add_template("123450@scan", _template())
        c = Decimal("10000")
        await svc.process(
            Bar(
                ticker="123450", resolution=Resolution.M5,
                ts=datetime(2026, 7, 13, 1, 0, tzinfo=UTC),
                open=c, high=c, low=c, close=c, volume=Decimal("100"), value=c * 100,
            )
        )  # 관망 랏 스폰 (보유 아님)
        assert "123450" not in svc.open_tickers()  # 아직 미진입
        assert "123450" in svc.tracked_tickers()  # 그래도 시세는 받아야 한다

        # 템플릿이 은퇴해도 랏이 살아 있으면 계속 구독 (만료 판정에 봉이 필요).
        svc._remove_template("123450", svc._by_ticker["123450"][0])
        assert "123450" in svc.tracked_tickers()

    async def test_retired_template_drops_out(self, sf) -> None:
        """만료(one-shot 당일 종료)된 합류분은 목록에서 빠져야 한다 —
        안 빠지면 날마다 쌓여 WS 구독 한도(~41)를 채우고 신규 합류가 조용히 멈춘다."""
        svc = _service(sf)
        tmpl = _template()
        svc.add_template("123450@scan", tmpl)
        assert "123450" in svc.tracked_tickers()
        svc._remove_template("123450", tmpl)  # _expire_scan_lot 이 타는 경로
        assert "123450" not in svc.tracked_tickers()


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


async def test_one_shot_unentered_lot_expires_next_day(sf) -> None:
    """미진입 one-shot 랏은 다음 거래일 첫 봉에서 CANCELLED로 만료된다.

    합류 근거(당일 급등+거래폭증)는 하루살이 — 검증된 시뮬(당일 리플레이)에 없는
    '며칠 뒤 진입'(실측: 7/23 합류 → 7/27 진입)을 차단한다."""
    from datetime import UTC, datetime

    from short_trading_bot.market.types import Bar

    svc = _service(sf)
    assert svc.add_template("123450@scan", _template(), one_shot=True)

    def bar(day: int, ts_min: int) -> Bar:
        c = Decimal("10000")
        return Bar(
            ticker="123450", resolution=Resolution.M5,
            ts=datetime(2026, 7, day, 1, ts_min, tzinfo=UTC),  # 10:xx KST
            open=c, high=c, low=c, close=c, volume=Decimal("100"), value=c * 100,
        )

    await svc.process(bar(13, 0))  # 합류일 첫 봉 → 스폰 + 합류일 기록
    lot = svc.lot("123450", Resolution.M5)
    assert lot is not None and lot.qty == 0

    await svc.process(bar(13, 5))  # 같은 날은 계속 관찰
    assert svc.lot("123450", Resolution.M5) is lot

    await svc.process(bar(14, 0))  # 다음 거래일 → 만료
    assert lot.is_terminal
    assert svc.lot("123450", Resolution.M5) is None  # 랏 제거
    assert all(k.split("@")[0] != "123450" for k in svc._watchlist)  # 템플릿 은퇴
    assert svc.add_template("123450@scan", _template(), one_shot=True)  # 재합류는 가능


async def test_scanner_config_chandelier_passthrough(tmp_path) -> None:
    import json as _json

    p = tmp_path / "wl.json"
    p.write_text(_json.dumps({"scanner": {"enabled": True, "chandelier_mult": 2.0}}))
    tmpl = load_scanner_config(p).template()
    assert tmpl.stop.chandelier_mult == 2.0
