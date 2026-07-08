import json
from decimal import Decimal

from short_trading_bot.app.watchlist import load_trading_config
from short_trading_bot.domain.enums import Market, Resolution


def test_load_trading_config(tmp_path) -> None:
    path = tmp_path / "wl.json"
    path.write_text(
        json.dumps(
            {
                "limits": {
                    "daily_loss_limit": "500000",
                    "daily_loss_pct": 0.03,
                    "max_drawdown_pct": 0.15,
                    "max_open_positions": 5,
                    "max_order_notional": "5000000",
                    "max_ticker_exposure": "10000000",
                },
                "watchlist": {
                    "005930": {
                        "strategy_id": "trend_long_v1",
                        "market": "KRX",
                        "resolution": "1D",
                        "risk_per_trade": 0.01,
                        "strategy_params": {"require_confirm": False},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    watchlist, limits = load_trading_config(path)

    assert "005930" in watchlist
    tmpl = watchlist["005930"]
    assert tmpl.strategy_id == "trend_long_v1"
    assert tmpl.market is Market.KRX
    assert tmpl.resolution is Resolution.D1
    assert tmpl.strategy_params == {"require_confirm": False}

    assert limits.daily_loss_limit == Decimal("500000")
    assert limits.daily_loss_pct == 0.03
    assert limits.max_drawdown_pct == 0.15
    assert limits.max_open_positions == 5
    assert limits.max_order_notional == Decimal("5000000")


def test_load_trading_config_empty_limits(tmp_path) -> None:
    path = tmp_path / "wl.json"
    path.write_text(json.dumps({"watchlist": {}}), encoding="utf-8")
    watchlist, limits = load_trading_config(path)
    assert watchlist == {}
    assert limits.daily_loss_limit is None
