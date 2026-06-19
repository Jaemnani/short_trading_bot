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
def serve() -> None:
    """Run the trading engine (placeholder until the asyncio orchestrator lands)."""
    s = _bootstrap()
    log = get_logger("serve")
    active = s.active_kis()
    if s.mode.value in {"PAPER", "LIVE"} and not active.configured:
        log.warning(
            "kis.credentials.missing",
            mode=s.mode.value,
            hint="set STB_KIS__PAPER__* or STB_KIS__LIVE__* in .env",
        )
    log.info("engine.start.placeholder", mode=s.mode.value, dry_run=s.dry_run)
    typer.echo("engine scaffold ready — wire a Feed + broker to TradingService (app/service.py).")


@app.command("api")
def serve_api(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Serve the dashboard API + WebSocket (FastAPI). Put HTTPS in front for production."""
    import uvicorn

    s = _bootstrap()
    get_logger("api").info("api.start", host=host, port=port, mode=s.mode.value)
    uvicorn.run("short_trading_bot.api.main:app", host=host, port=port)


@campaign_app.command("list")
def campaign_list() -> None:
    """List campaigns (placeholder)."""
    _bootstrap()
    typer.echo("no campaigns yet — campaign engine lands in P8.")


if __name__ == "__main__":
    app()
