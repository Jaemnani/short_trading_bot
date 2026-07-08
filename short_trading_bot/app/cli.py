"""Command-line entrypoint (``trader``).

P0 wires config + logging + DB init and a ``serve`` placeholder. The asyncio engine
orchestrator (feed/indicator/strategy/order/reconciler) lands in later phases.
"""

from __future__ import annotations

import asyncio

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

    from ..domain.enums import Resolution
    from ..execution.broker.paper import PaperBrokerAdapter
    from ..infra.kis_auth import KisAuth
    from ..market.kis_ws_feed import KisWebSocketFeed
    from .engine import build_broker, build_trading_service, kis_rest_base, kis_ws_url
    from .watchlist import load_trading_config

    s = _bootstrap()
    log = get_logger("serve")
    watchlist, limits = load_trading_config(config)
    if not watchlist:
        typer.echo(f"watchlist empty — add entries to {config} (see watchlist.example.json).")
        raise typer.Exit(1)

    resolutions = {tmpl.resolution for tmpl in watchlist.values()}
    if len(resolutions) > 1:
        # One WS feed aggregates at one timeframe; mixed configs would evaluate lots on
        # wrong-timeframe bars (the lot-level guard would then just HOLD forever).
        typer.echo(
            f"watchlist mixes resolutions {sorted(r.value for r in resolutions)} — "
            "run one serve process per resolution (separate config files)."
        )
        raise typer.Exit(1)

    creds = s.active_kis()
    if not creds.configured:
        typer.echo("No KIS keys in .env — cannot stream live data. Fill STB_KIS__* then re-run.")
        raise typer.Exit(1)

    from ..infra.notifier.factory import build_notifier

    broker = build_broker(s) if live_exec else PaperBrokerAdapter()
    service = build_trading_service(
        s, watchlist, limits=limits, broker=broker, notifier=build_notifier(s)
    )

    async def _poll_loop(poller: object) -> None:
        from ..execution.fill_poller import FillPoller

        assert isinstance(poller, FillPoller)
        while True:
            try:
                await poller.poll_once()
            except Exception:
                log.exception("fill_poll.error")
            await asyncio.sleep(fill_poll_seconds)

    def _backfill_daily() -> int:
        """일봉 워밍업 백필 (FinanceDataReader, 최근 ~200일)."""
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        import FinanceDataReader as fdr

        from ..market.types import Bar

        start = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%d")
        bars: list[Bar] = []
        for ticker in watchlist:
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

    async def _backfill_intraday(auth: KisAuth, resolution: Resolution) -> int:
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
        for ticker in watchlist:
            try:
                one_min = await hist.fetch_days(ticker, days)
            except Exception:
                log.warning("backfill.intraday_failed", ticker=ticker)
                continue
            total += service.prime(
                one_min if resolution is Resolution.M1 else resample(one_min, resolution)
            )
        return total

    async def _run() -> None:
        restored = await service.hydrate()
        if live_exec:
            report = await service.reconcile()
            if not report.in_sync:
                log.warning("reconcile.drift_on_start", mismatches=len(report.mismatches))
        auth = KisAuth(creds, kis_rest_base(s.mode))
        resolution = Resolution(next(iter(watchlist.values())).resolution)
        # 지표 워밍업 백필 — 없으면 일봉 전략은 수십 거래일간 관망만 한다.
        if resolution is Resolution.D1:
            primed = _backfill_daily()
        elif resolution.is_intraday and resolution is not Resolution.TICK:
            primed = await _backfill_intraday(auth, resolution)
        else:
            primed = 0
        log.info("backfill.primed", bars=primed, resolution=resolution.value)
        log.info(
            "engine.start",
            mode=s.mode.value,
            tickers=len(watchlist),
            restored=restored,
            execution="live" if live_exec else "simulated",
        )
        poll_task = (
            asyncio.create_task(_poll_loop(service.make_fill_poller())) if live_exec else None
        )
        try:
            # Reconnect loop: a WS disconnect ends the stream; resume until 긴급중지.
            while not service.control.is_stopped:
                approval = await auth.approval_key()
                feed = KisWebSocketFeed(
                    approval, list(watchlist), resolution, ws_url=kis_ws_url(s.mode)
                )
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
