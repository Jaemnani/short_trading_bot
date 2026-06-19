from typer.testing import CliRunner

from short_trading_bot.app.cli import app
from short_trading_bot.infra.config import get_settings

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_config_command() -> None:
    get_settings.cache_clear()
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "mode" in result.stdout


def test_initdb_creates_tables(tmp_path, monkeypatch) -> None:
    db_file = tmp_path / "cli.db"
    monkeypatch.setenv("STB_DB_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    try:
        result = runner.invoke(app, ["initdb"])
        assert result.exit_code == 0, result.output
        assert db_file.exists()
    finally:
        get_settings.cache_clear()
