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

    creds = s.active_kis()
    if not creds.configured:
        typer.echo("No KIS keys in .env — cannot stream live data. Fill STB_KIS__* then re-run.")
        raise typer.Exit(1)

    broker = build_broker(s) if live_exec else PaperBrokerAdapter()
    service = build_trading_service(s, watchlist, limits=limits, broker=broker)

    async def _poll_loop(poller: object) -> None:
        from ..execution.fill_poller import FillPoller

        assert isinstance(poller, FillPoller)
        while True:
            try:
                await poller.poll_once()
            except Exception:
                log.exception("fill_poll.error")
            await asyncio.sleep(fill_poll_seconds)

    async def _run() -> None:
        restored = await service.hydrate()
        if live_exec:
            report = await service.reconcile()
            if not report.in_sync:
                log.warning("reconcile.drift_on_start", mismatches=len(report.mismatches))
        auth = KisAuth(creds, kis_rest_base(s.mode))
        approval = await auth.approval_key()
        resolution = Resolution(next(iter(watchlist.values())).resolution)
        feed = KisWebSocketFeed(approval, list(watchlist), resolution, ws_url=kis_ws_url(s.mode))
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
            await service.run(feed)
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
    """List campaigns (placeholder)."""
    _bootstrap()
    typer.echo("no campaigns yet — campaign engine lands in P8.")


if __name__ == "__main__":
    app()
