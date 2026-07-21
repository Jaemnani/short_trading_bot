"""MarketRegime (시장 레짐 필터) unit tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from short_trading_bot.market.regime import MarketRegime


def test_daily_flag_gates_entries() -> None:
    r = MarketRegime()
    assert r.entries_allowed  # 기본값: 허용
    r.set_daily(False)
    assert not r.entries_allowed
    r.set_daily(True)
    assert r.entries_allowed


def test_intraday_drop_blocks_and_stays_blocked() -> None:
    r = MarketRegime(proxy_leverage=2.0, daily_drop_pct=0.015)
    d = date(2026, 7, 22)
    r.set_daily(True, proxy_prev_close=Decimal("10000"))
    r.on_proxy_bar(d, Decimal("9900"))  # ETF -1% → 지수 -0.5%: 허용 유지
    assert r.entries_allowed
    r.on_proxy_bar(d, Decimal("9690"))  # ETF -3.1% → 지수 -1.55%: 차단
    assert not r.entries_allowed
    r.on_proxy_bar(d, Decimal("10100"))  # 당일 내 회복해도 재개하지 않음 (sticky)
    assert not r.entries_allowed


def test_new_day_resets_intraday_and_rolls_prev_close() -> None:
    r = MarketRegime(proxy_leverage=2.0, daily_drop_pct=0.015)
    d1, d2 = date(2026, 7, 22), date(2026, 7, 23)
    r.set_daily(True, proxy_prev_close=Decimal("10000"))
    r.on_proxy_bar(d1, Decimal("9600"))  # -2% 지수 → 차단
    assert not r.entries_allowed
    r.on_proxy_bar(d2, Decimal("9650"))  # 새 날: 장중 플래그 리셋
    assert r.entries_allowed
    # 새 날의 전일 종가는 어제 마지막 봉(9600)으로 롤오버 — 9650은 +0.26%라 허용 유지
