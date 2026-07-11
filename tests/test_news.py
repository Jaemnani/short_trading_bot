from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from short_trading_bot.app.service import TradingService
from short_trading_bot.domain.enums import PositionState, Resolution
from short_trading_bot.execution.broker.paper import PaperBrokerAdapter, PaperConfig
from short_trading_bot.market.feed import ReplayFeed
from short_trading_bot.market.types import Bar
from short_trading_bot.news.aggregator import SentimentAggregator
from short_trading_bot.news.dart import DartPoller
from short_trading_bot.news.mapper import TickerMapper
from short_trading_bot.news.rss import RssPoller
from short_trading_bot.news.sentiment import LexiconScorer
from short_trading_bot.news.service import NewsService
from short_trading_bot.news.types import NewsArticle
from short_trading_bot.risk.limits import RiskLimits
from short_trading_bot.risk.manager import RiskManager
from short_trading_bot.strategy.templates import StrategyTemplate

T0 = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)


# --- LexiconScorer ---

def test_lexicon_scorer() -> None:
    s = LexiconScorer()
    assert s.score("삼성전자 자기주식취득 결정") > 0
    assert s.score("코스닥 상장폐지 사유 발생") < 0
    assert s.score("정기 주주총회 소집 공고") == 0.0  # neutral -> dead-band


# --- TickerMapper ---

def test_mapper_prefers_stock_code() -> None:
    m = TickerMapper({"삼성전자": "005930"})
    art = NewsArticle(id="1", source="DART", title="유상증자", url="", stock_code="000660")
    assert m.map(art) == "000660"  # authoritative wins over name match


def test_mapper_name_match_and_miss() -> None:
    m = TickerMapper({"삼성전자": "005930", "삼성전자우": "005935"})
    assert m.map(NewsArticle(id="1", source="RSS", title="삼성전자우 강세", url="")) == "005935"
    assert m.map(NewsArticle(id="2", source="RSS", title="없는회사 급등", url="")) is None


# --- SentimentAggregator ---

def test_aggregator_blends_and_decays() -> None:
    agg = SentimentAggregator(half_life_hours=4.0, alpha=0.5)
    agg.ingest("005930", 1.0, T0)
    assert agg.ewma("005930") == pytest.approx(0.5)  # 0 -> blend 1.0
    agg.ingest("005930", 1.0, T0)  # same ts, no decay
    assert agg.ewma("005930") == pytest.approx(0.75)
    # query one half-life later -> decays toward neutral
    assert agg.ewma("005930", now=T0 + timedelta(hours=4)) == pytest.approx(0.375)


def test_aggregator_halt_keyword() -> None:
    agg = SentimentAggregator()
    agg.ingest("005930", -0.5, T0, title="상장폐지 사유 발생")
    assert agg.halted("005930")
    assert agg.ewma("005930") == -1.0


# --- pollers ---

async def test_dart_poller_dedup_and_flags() -> None:
    rows: list[dict[str, Any]] = [
        {"rcept_no": "001", "report_nm": "유상증자 결정", "stock_code": "005930", "pblntf_ty": "B"},
        {"rcept_no": "002", "report_nm": "분기보고서", "stock_code": "000660", "pblntf_ty": "A"},
    ]

    async def fetch() -> list[dict[str, Any]]:
        return rows

    poller = DartPoller(fetch)
    first = await poller.poll()
    assert len(first) == 2
    assert first[0].stock_code == "005930"
    assert DartPoller.is_high_impact(first[0]) is True  # type B
    assert DartPoller.is_high_impact(first[1]) is False  # type A
    assert await poller.poll() == []  # dedup by rcept_no


async def test_rss_poller_dedup() -> None:
    entries = [
        {"link": "http://x/1", "title": "삼성전자 신고가"},
        {"link": "http://x/1", "title": "삼성전자 신고가"},  # duplicate wire story
    ]

    async def fetch() -> list[dict[str, Any]]:
        return entries

    poller = RssPoller(fetch, outlet="hankyung")
    out = await poller.poll()
    assert len(out) == 1 and out[0].source == "RSS:hankyung"


async def test_news_service_integration() -> None:
    async def dart_fetch() -> list[dict[str, Any]]:
        return [{"rcept_no": "001", "report_nm": "유상증자 결정", "stock_code": "005930", "pblntf_ty": "B"}]

    svc = NewsService([DartPoller(dart_fetch)], TickerMapper())
    n = await svc.poll_once(T0)
    assert n == 1
    assert (svc.ewma("005930") or 0) < 0  # 유상증자 -> negative


# --- P4 <-> P9 wiring: news EWMA gates entry ---

def _uptrend() -> list[Bar]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    out = []
    for i in range(80):
        c = Decimal(str(100.0 + 2 * i))
        out.append(Bar("005930", Resolution.D1, base + timedelta(days=i), c, c, c - 1, c, Decimal("1000"), c * Decimal("1000")))
    return out


async def test_negative_news_blocks_entry(sf) -> None:
    broker = PaperBrokerAdapter(PaperConfig(initial_cash=Decimal("100000000")))
    watchlist = {
        "005930": StrategyTemplate(
            strategy_id="trend_long_v1", resolution=Resolution.D1,
            strategy_params={"require_confirm": False},
        )
    }
    svc = TradingService(
        broker, sf, RiskManager(RiskLimits()), watchlist,
        news_provider=lambda _ticker: -0.5,  # below default news_block -0.3
    )
    await svc.run(ReplayFeed(_uptrend()))

    assert svc.lot("005930").state is PositionState.WATCHING  # entry vetoed by news
    assert (await broker.get_balance()).positions == []
