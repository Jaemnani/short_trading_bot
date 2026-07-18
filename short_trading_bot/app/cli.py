"""Command-line entrypoint (``trader``).

P0 wires config + logging + DB init and a ``serve`` placeholder. The asyncio engine
orchestrator (feed/indicator/strategy/order/reconciler) lands in later phases.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer

from ..infra.config import Settings, get_settings
from ..infra.logging import configure_logging, get_logger
from ..persistence.db import create_engine, init_models

app = typer.Typer(no_args_is_help=True, help="short_trading_bot — KIS 자동매매 봇 CLI")
campaign_app = typer.Typer(no_args_is_help=True, help="기간 한정 운용(Campaign) 관리")
app.add_typer(campaign_app, name="campaign")


def _bootstrap() -> Settings:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return settings


@app.command()
def version() -> None:
    """Print the package version."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    try:
        typer.echo(pkg_version("short-trading-bot"))
    except PackageNotFoundError:
        typer.echo("0.0.1 (dev)")


@app.command()
def config() -> None:
    """Show effective settings (secrets masked)."""
    s = _bootstrap()
    typer.echo(f"mode            = {s.mode.value}")
    typer.echo(f"dry_run         = {s.dry_run}")
    typer.echo(f"db_url          = {s.db_url}")
    typer.echo(f"log             = {s.log_level}/{s.log_format}")
    typer.echo(f"kis.paper       = {'configured' if s.kis.paper.configured else 'MISSING'}")
    typer.echo(f"kis.live        = {'configured' if s.kis.live.configured else 'MISSING'}")
    typer.echo(f"dart_api_key    = {'set' if s.dart_api_key else 'unset'}")
    typer.echo(f"active env       = {'live' if s.is_live else 'paper'}")


@app.command()
def initdb() -> None:
    """Create database tables (dev/tests; Alembic owns prod schema)."""
    s = _bootstrap()
    log = get_logger("initdb")

    async def _run() -> None:
        engine = create_engine(s.db_url)
        await init_models(engine)
        await engine.dispose()

    asyncio.run(_run())
    log.info("db.initialized", db_url=s.db_url)


@app.command()
def serve(
    config: str = "watchlist.json",
    live_exec: bool = False,
    fill_poll_seconds: float = 2.0,
) -> None:
    """Run the engine: load watchlist+limits, hydrate from DB, stream the KIS feed.

    Default execution is SIMULATED (PaperBroker) on live KIS data — a forward test.
    --live-exec uses the real KIS broker (routing 국내/해외) + a FillPoller (polls 체결내역,
    the authoritative fill record) + a startup reconcile against 잔고.
    """
    import asyncio

    from ..domain.enums import Mode, Resolution
    from ..execution.broker.paper import PaperBrokerAdapter
    from ..infra.kis_auth import KisAuth
    from ..market.kis_ws_feed import KisWebSocketFeed
    from .engine import build_broker, build_trading_service, kis_rest_base, kis_ws_url
    from .watchlist import load_paper_cash, load_scanner_config, load_trading_config

    s = _bootstrap()
    log = get_logger("serve")
    watchlist, limits = load_trading_config(config)
    scanner_cfg = load_scanner_config(config)
    paper_cash = load_paper_cash(config)
    if not watchlist and not scanner_cfg.enabled:
        typer.echo(f"watchlist empty — add entries to {config} (see watchlist.example.json).")
        raise typer.Exit(1)

    # 멀티 해상도: 한 WS 연결(틱)에서 필요한 모든 해상도의 봉을 동시에 집계한다.
    wanted = {tmpl.resolution for tmpl in watchlist.values()}
    if scanner_cfg.enabled:
        wanted.add(scanner_cfg.template().resolution)  # 스캐너 합류분의 봉 집계용
    resolutions = sorted(wanted, key=lambda r: r.value)
    tickers = sorted({key.split("@")[0] for key in watchlist})

    creds = s.active_kis()
    if not creds.configured:
        typer.echo("No KIS keys in .env — cannot stream live data. Fill STB_KIS__* then re-run.")
        raise typer.Exit(1)

    from ..infra.notifier.factory import build_notifier

    if live_exec:
        broker = build_broker(s)
    else:
        from ..execution.broker.paper import PaperConfig

        broker = PaperBrokerAdapter(
            PaperConfig(initial_cash=paper_cash) if paper_cash else PaperConfig()
        )
    notifier = build_notifier(s)
    service = build_trading_service(
        s, watchlist, limits=limits, broker=broker, notifier=notifier
    )
    feed_ref: dict[str, KisWebSocketFeed | None] = {"feed": None}  # 스캐너 동적 구독용

    async def _poll_loop(poller: object) -> None:
        from ..execution.fill_poller import FillPoller

        assert isinstance(poller, FillPoller)
        # UNKNOWN 주문 복구는 같은 주기로 동행 — UNKNOWN 이 없으면 API 호출 없이 즉시 반환.
        resolver = service.make_unknown_resolver()
        while True:
            try:
                await resolver.poll_once()
            except Exception:
                log.exception("unknown_resolve.error")
            try:
                await poller.poll_once()
            except Exception:
                log.exception("fill_poll.error")
            await asyncio.sleep(fill_poll_seconds)

    def _backfill_daily(daily_tickers: list[str]) -> int:
        """일봉 워밍업 백필 (FinanceDataReader, 최근 ~200일)."""
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        import FinanceDataReader as fdr

        from ..market.types import Bar

        start = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%d")
        bars: list[Bar] = []
        for ticker in daily_tickers:
            try:
                df = fdr.DataReader(ticker, start)
            except Exception:
                log.warning("backfill.failed", ticker=ticker)
                continue
            for idx, row in df.iterrows():
                if row.isna().any() or row["Volume"] == 0:
                    continue
                c = Decimal(str(row["Close"]))
                v = Decimal(str(int(row["Volume"])))
                bars.append(
                    Bar(ticker, Resolution.D1,
                        datetime(idx.year, idx.month, idx.day, tzinfo=UTC),
                        Decimal(str(row["Open"])), Decimal(str(row["High"])),
                        Decimal(str(row["Low"])), c, v, c * v)
                )
        return service.prime(bars)

    async def _backfill_intraday(
        auth: KisAuth, resolution: Resolution, res_tickers: list[str]
    ) -> int:
        """분봉 워밍업 백필 (KIS 분봉, 최근 5거래일 — 디스크 캐시 우선)."""
        from datetime import date, timedelta

        from ..market.kis_history import KisMinuteHistory, resample

        hist = KisMinuteHistory(auth, creds, kis_rest_base(s.mode))
        days: list[date] = []
        d = date.today() - timedelta(days=1)
        while len(days) < 5:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        total = 0
        for ticker in res_tickers:
            try:
                one_min = await hist.fetch_days(ticker, days)
            except Exception:
                log.warning("backfill.intraday_failed", ticker=ticker)
                continue
            total += service.prime(
                one_min if resolution is Resolution.M1 else resample(one_min, resolution)
            )
        return total

    def _tickers_for(resolution: Resolution) -> list[str]:
        return sorted({
            key.split("@")[0] for key, tmpl in watchlist.items() if tmpl.resolution is resolution
        })

    # -- 장중 종목검색(스캐너): 급등+거래량 급증 종목 자동 합류 ---------------------

    def _refresh_favorites() -> set[str]:
        """며칠 일봉 스캔(거래량 상승 + 우상향) 후보군 — 완화 문턱으로 우선 합류.

        FDR 다운로드가 느려서(수 분) executor 스레드에서 하루 1회 돈다."""
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        import FinanceDataReader as fdr

        from ..market.scanner import scan_volume_leaders
        from ..market.types import Bar

        start = (datetime.now(UTC) - timedelta(days=120)).strftime("%Y-%m-%d")
        candidates: dict[str, tuple[str, list[Bar]]] = {}
        for market_name in ("KOSPI", "KOSDAQ"):
            try:
                listing = fdr.StockListing(market_name)
            except Exception:
                log.warning("scanner.listing_failed", market=market_name)
                continue
            if "Marcap" in listing.columns:
                listing = listing.sort_values("Marcap", ascending=False)
            for _, row in listing.head(scanner_cfg.daily_universe // 2).iterrows():
                code, name = str(row["Code"]), str(row["Name"])
                try:
                    df = fdr.DataReader(code, start)
                except Exception:
                    continue
                bars = []
                for idx, r in df.iterrows():
                    if r.isna().any() or r["Volume"] == 0:
                        continue
                    c = Decimal(str(r["Close"]))
                    v = Decimal(str(int(r["Volume"])))
                    bars.append(
                        Bar(code, Resolution.D1,
                            datetime(idx.year, idx.month, idx.day, tzinfo=UTC),
                            Decimal(str(r["Open"])), Decimal(str(r["High"])),
                            Decimal(str(r["Low"])), c, v, c * v)
                    )
                candidates[code] = (name, bars)
        leaders = scan_volume_leaders(candidates, top=scanner_cfg.daily_candidates)
        return {r.ticker for r in leaders}

    def _record_rankings(now: object, rows: list[Any]) -> None:
        """순위 스냅샷을 JSONL로 축적 — 실데이터 기반 사후 검증/재시뮬레이션의 원천."""
        import dataclasses
        import json as _json
        from pathlib import Path

        out = Path("data/rankings") / f"{now:%Y%m%d}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(
                {"ts": f"{now:%H:%M:%S}", "rows": [dataclasses.asdict(r) for r in rows]},
                ensure_ascii=False,
            ) + "\n")

    async def _scan_loop(auth: KisAuth) -> None:
        from datetime import date as _date
        from datetime import datetime, timedelta, timezone

        from ..market.kis_history import KisMinuteHistory, resample
        from ..market.kis_ranking import KisVolumeRank
        from ..market.scanner import MomentumPick, pick_momentum

        kst = timezone(timedelta(hours=9))
        scan_res = scanner_cfg.template().resolution
        # 순위분석 API는 모의 도메인 미지원 가능 → 라이브 키가 있으면 라이브 도메인으로
        # 시세만 조회한다 (주문과 무관, 안전).
        if s.kis.live.configured and s.mode is not Mode.LIVE:
            rank_creds = s.kis.live
            rank_base = kis_rest_base(Mode.LIVE)
            rank_auth = KisAuth(rank_creds, rank_base)
        else:
            rank_creds, rank_base, rank_auth = creds, kis_rest_base(s.mode), auth
        ranking = KisVolumeRank(rank_auth, rank_creds, rank_base)
        hist = KisMinuteHistory(auth, creds, kis_rest_base(s.mode))
        favorites: set[str] = set()
        fav_day: _date | None = None
        joined_today: dict[_date, int] = {}

        async def _join(pick: MomentumPick, today: _date) -> bool:
            # 지표 워밍업: 오늘 1분봉을 캐시 없이 받아 스캔 해상도로 리샘플 후 프라임.
            try:
                one_min = await hist.fetch_day(pick.ticker, today, cache=False)
            except Exception:
                log.exception("scanner.backfill_failed", ticker=pick.ticker)
                return False
            service.prime(one_min if scan_res is Resolution.M1 else resample(one_min, scan_res))
            if not service.add_template(
                f"{pick.ticker}@scan", scanner_cfg.template(), one_shot=not scanner_cfg.rejoin
            ):
                return False
            feed = feed_ref["feed"]
            if feed is not None:
                await feed.subscribe(pick.ticker)
            elif pick.ticker not in tickers:
                tickers.append(pick.ticker)
            await notifier.notify(
                "scanner.joined", ticker=pick.ticker, name=pick.name,
                change_pct=f"{pick.change_pct:.1f}", vol_surge=f"{pick.vol_surge:.0f}",
                favorite=str(pick.favorite),
            )
            log.info("scanner.joined", ticker=pick.ticker, name=pick.name,
                     change_pct=pick.change_pct, favorite=pick.favorite)
            return True

        while True:
            try:
                now = datetime.now(kst)
                minute = now.hour * 60 + now.minute
                in_session = now.weekday() < 5 and (9 * 60 + 5) <= minute <= (14 * 60)
                if in_session and not service.control.is_stopped:
                    today = now.date()
                    if scanner_cfg.daily_candidates and fav_day != today:
                        fav_day = today
                        try:
                            favorites = await asyncio.get_running_loop().run_in_executor(
                                None, _refresh_favorites
                            )
                            log.info("scanner.favorites", count=len(favorites))
                        except Exception:
                            log.exception("scanner.favorites_failed")
                    capacity = scanner_cfg.max_active - joined_today.get(today, 0)
                    # KIS WS 등록 한도(~41) 보호: 여유 없으면 이번 주기는 건너뜀.
                    if capacity > 0 and len(tickers) < 40:
                        rows = await ranking.top()
                        if scanner_cfg.record_rankings and rows:
                            _record_rankings(now, rows)  # 사후 검증/재시뮬레이션용 스냅샷
                        picks = pick_momentum(
                            rows,
                            min_change_pct=scanner_cfg.min_change_pct,
                            max_change_pct=scanner_cfg.max_change_pct,
                            min_vol_surge=scanner_cfg.min_vol_surge,
                            min_value=scanner_cfg.min_value_traded,
                            exclude=set(tickers),
                            favorites=favorites,
                            favorite_relax=scanner_cfg.favorite_relax,
                            top=capacity,
                        )
                        for pick in picks:
                            if await _join(pick, today):
                                joined_today[today] = joined_today.get(today, 0) + 1
            except Exception:
                log.exception("scanner.error")
            await asyncio.sleep(scanner_cfg.interval_seconds)

    async def _run() -> None:
        restored = await service.hydrate()
        # 스캐너로 합류했던(워치리스트 밖) 열린 랏도 재시작 후 시세를 받아야 관리된다.
        for extra in sorted(service.open_tickers() - set(tickers)):
            tickers.append(extra)
        if live_exec:
            report = await service.reconcile()
            if not report.in_sync:
                log.warning("reconcile.drift_on_start", mismatches=len(report.mismatches))
        auth = KisAuth(creds, kis_rest_base(s.mode))
        # 지표 워밍업 백필 — 해상도별로. 없으면 저빈도 전략은 수십 거래일간 관망만 한다.
        primed = 0
        for res in resolutions:
            res_tickers = _tickers_for(res)
            if res is Resolution.D1:
                primed += _backfill_daily(res_tickers)
            elif res.is_intraday and res is not Resolution.TICK:
                primed += await _backfill_intraday(auth, res, res_tickers)
        log.info(
            "backfill.primed", bars=primed,
            resolutions=[r.value for r in resolutions],
        )
        log.info(
            "engine.start",
            mode=s.mode.value,
            tickers=len(tickers),
            entries=len(watchlist),
            resolutions=[r.value for r in resolutions],
            restored=restored,
            execution="live" if live_exec else "simulated",
        )
        poll_task = (
            asyncio.create_task(_poll_loop(service.make_fill_poller())) if live_exec else None
        )
        scan_task = asyncio.create_task(_scan_loop(auth)) if scanner_cfg.enabled else None
        from ..market.bar_builder import BarBuilder

        # 해상도별 공유 빌더: 한 WS 연결의 틱을 모든 해상도로 동시 집계, 재접속에도 봉 보존.
        shared_builders = [BarBuilder(res) for res in resolutions]
        try:
            # Reconnect loop: a WS disconnect ends the stream; resume until 긴급중지.
            while not service.control.is_stopped:
                approval = await auth.approval_key()
                feed = KisWebSocketFeed(
                    approval, tickers, resolutions[0], ws_url=kis_ws_url(s.mode),
                    bar_builders=shared_builders, flush_on_close=False,
                )
                feed_ref["feed"] = feed  # 스캐너가 현재 연결에 동적 구독할 수 있도록
                try:
                    await service.run(feed)
                except Exception:
                    log.exception("feed.error")
                if service.control.is_stopped:
                    break
                if live_exec:  # recover anything missed while disconnected
                    await service.make_fill_poller().poll_once()
                    await service.reconcile()
                log.info("feed.reconnect", delay_seconds=5)
                await asyncio.sleep(5)
        finally:
            if poll_task is not None:
                poll_task.cancel()
            if scan_task is not None:
                scan_task.cancel()

    asyncio.run(_run())


@app.command("api")
def serve_api(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Serve the dashboard API + WebSocket (FastAPI). Put HTTPS in front for production."""
    import uvicorn

    s = _bootstrap()
    get_logger("api").info("api.start", host=host, port=port, mode=s.mode.value)
    uvicorn.run("short_trading_bot.api.main:app", host=host, port=port)


@app.command()
def scan(
    market: str = "KOSPI",
    limit: int = 100,
    top: int = 15,
    days: int = 120,
    min_vol_ratio: float = 1.2,
) -> None:
    """거래량 상승 + 우상향 종목 스캔 (watchlist 후보). 시총 상위 limit개 대상."""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    try:
        import FinanceDataReader as fdr
    except ImportError:
        typer.echo("FinanceDataReader가 필요합니다: .venv/bin/pip install finance-datareader")
        raise typer.Exit(1) from None

    from ..domain.enums import Resolution
    from ..market.scanner import scan_volume_leaders
    from ..market.types import Bar

    _bootstrap()
    listing = fdr.StockListing(market)
    if "Marcap" in listing.columns:
        listing = listing.sort_values("Marcap", ascending=False)
    listing = listing.head(limit)
    start = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")

    candidates: dict[str, tuple[str, list[Bar]]] = {}
    for _, row in listing.iterrows():
        code, name = str(row["Code"]), str(row["Name"])
        try:
            df = fdr.DataReader(code, start)
        except Exception:
            continue
        bars = []
        for idx, r in df.iterrows():
            if r.isna().any() or r["Volume"] == 0:
                continue
            c = Decimal(str(r["Close"]))
            v = Decimal(str(int(r["Volume"])))
            bars.append(
                Bar(
                    ticker=code, resolution=Resolution.D1,
                    ts=datetime(idx.year, idx.month, idx.day, tzinfo=UTC),
                    open=Decimal(str(r["Open"])), high=Decimal(str(r["High"])),
                    low=Decimal(str(r["Low"])), close=c, volume=v, value=c * v,
                )
            )
        candidates[code] = (name, bars)

    leaders = scan_volume_leaders(candidates, min_vol_ratio=min_vol_ratio, top=top)
    if not leaders:
        typer.echo("조건에 맞는 종목 없음 (거래량 증가 + 우상향)")
        return
    typer.echo(f"{'코드':<8}{'종목':<12}{'거래량비':>8}{'당일RVOL':>10}{'거래대금(억)':>12}{'종가':>10}")
    for r in leaders:
        typer.echo(
            f"{r.ticker:<8}{r.name:<12}{r.vol_ratio:>7.2f}x{r.rvol_today:>9.2f}x"
            f"{r.avg_value / 1e8:>11.0f}{r.close:>10,.0f}"
        )


@app.command()
def preflight() -> None:
    """Go-live readiness checks (run before flipping STB_MODE=LIVE)."""
    from .engine import is_ready
    from .engine import preflight as run_preflight

    s = _bootstrap()
    checks = run_preflight(s)
    for c in checks:
        mark = "OK" if c.ok else ("!!" if c.critical else "--")
        typer.echo(f"[{mark}] {c.name}: {c.detail}")
    ready = is_ready(checks)
    typer.echo(f"\nready={ready} (mode={s.mode.value})")
    if not ready:
        raise typer.Exit(code=1)


@campaign_app.command("list")
def campaign_list() -> None:
    """List campaigns stored in the DB."""
    import asyncio

    from sqlalchemy import select

    from ..persistence.db import create_engine, session_factory, session_scope
    from ..persistence.models import Campaign

    s = _bootstrap()

    async def _list() -> list[Campaign]:
        engine = create_engine(s.db_url)
        try:
            async with session_scope(session_factory(engine)) as session:
                return list((await session.execute(select(Campaign))).scalars().all())
        finally:
            await engine.dispose()

    rows = asyncio.run(_list())
    if not rows:
        typer.echo("no campaigns yet — run one via CampaignManager (backtest) or the API.")
        return
    for c in rows:
        typer.echo(
            f"{c.campaign_id}  {c.name}  [{c.status}]  budget={c.initial_budget} {c.currency}  "
            f"{c.start_at:%Y-%m-%d} ~ {c.end_at:%Y-%m-%d}"
        )


if __name__ == "__main__":
    app()
