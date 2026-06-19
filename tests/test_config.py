from short_trading_bot.domain.enums import Mode
from short_trading_bot.infra.config import Settings


def test_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.mode is Mode.PAPER
    assert s.dry_run is True
    assert not s.kis.paper.configured
    assert not s.is_live
    assert s.active_kis() is s.kis.paper


def test_nested_env_override(monkeypatch) -> None:
    monkeypatch.setenv("STB_MODE", "LIVE")
    monkeypatch.setenv("STB_KIS__LIVE__APP_KEY", "k")
    monkeypatch.setenv("STB_KIS__LIVE__APP_SECRET", "s")
    monkeypatch.setenv("STB_KIS__LIVE__ACCOUNT_NO", "12345678-01")
    s = Settings(_env_file=None)
    assert s.is_live
    active = s.active_kis()
    assert active is s.kis.live
    assert active.configured
    assert active.app_key == "k"
    assert active.account_product_code == "01"  # default preserved
