"""KIS 분봉 히스토리 로더 offline tests (fake transport, 실측 스키마 기반)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from short_trading_bot.domain.enums import Resolution
from short_trading_bot.infra.config import KisEnvCreds
from short_trading_bot.infra.kis_auth import KisAuth
from short_trading_bot.market.kis_history import KisMinuteHistory, resample

DAY = date(2026, 7, 8)


def _row(hhmmss: str, price: str, vol: str = "100") -> dict[str, str]:
    return {
        "stck_bsop_date": "20260708", "stck_cntg_hour": hhmmss,
        "stck_prpr": price, "stck_oprc": price, "stck_hgpr": price, "stck_lwpr": price,
        "cntg_vol": vol,
    }


def _auth() -> KisAuth:
    async def fetch() -> tuple[str, int]:
        return "tok", 86400

    creds = KisEnvCreds(app_key="k", app_secret="s", account_no="1-01")
    return KisAuth(creds, "https://x", token_fetcher=fetch)


def _history(pages: list[list[dict[str, str]]], tmp_path) -> tuple[KisMinuteHistory, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def transport(method: str, url: str, headers: dict, params: dict) -> dict[str, Any]:
        calls.append(params)
        idx = len(calls) - 1
        rows = pages[idx] if idx < len(pages) else []
        return {"rt_cd": "0", "output2": rows}

    creds = KisEnvCreds(app_key="k", app_secret="s", account_no="1-01")
    h = KisMinuteHistory(_auth(), creds, "https://x", transport=transport,
                         cache_dir=tmp_path, rate_delay=0.0)
    return h, calls


async def test_fetch_day_paginates_and_sorts(tmp_path) -> None:
    # 페이지1: 15:30~15:29 (내림차순), 페이지2: 09:01~09:00 → 커서가 1분 전으로 이동
    pages = [
        [_row("153000", "1000"), _row("152900", "999")],
        [_row("090100", "990"), _row("090000", "991")],
    ]
    h, calls = _history(pages, tmp_path)
    bars = await h.fetch_day("005930", DAY)

    assert [b.ts.strftime("%H%M%S") for b in bars] == ["090000", "090100", "152900", "153000"]
    assert bars[0].close == Decimal("991")
    assert bars[0].resolution is Resolution.M1
    assert calls[0]["FID_INPUT_HOUR_1"] == "153000"
    assert calls[1]["FID_INPUT_HOUR_1"] == "152800"  # 최저시각(152900) - 1분


async def test_fetch_day_caches_to_disk(tmp_path) -> None:
    pages = [[_row("090000", "100", "5")]]
    h, calls = _history(pages, tmp_path)
    first = await h.fetch_day("005930", DAY)
    assert len(first) == 1 and len(calls) >= 1

    calls_before = len(calls)
    again = await h.fetch_day("005930", DAY)  # 캐시 히트 — API 호출 없음
    assert len(calls) == calls_before
    assert [b.close for b in again] == [b.close for b in first]


async def test_fetch_day_failure_not_cached(tmp_path) -> None:
    async def transport(method: str, url: str, headers: dict, params: dict) -> dict[str, Any]:
        return {"rt_cd": "1", "msg1": "error"}

    creds = KisEnvCreds(app_key="k", app_secret="s", account_no="1-01")
    h = KisMinuteHistory(_auth(), creds, "https://x", transport=transport,
                         cache_dir=tmp_path, rate_delay=0.0)
    assert await h.fetch_day("005930", DAY) == []
    assert not (tmp_path / "005930").exists()  # 실패는 캐시하지 않음 (재시도 가능)


def test_resample_1m_to_5m() -> None:
    rows = [_row(f"09{m:02d}00", str(100 + m), "10") for m in range(10)]  # 09:00~09:09
    bars_1m = KisMinuteHistory.parse_rows("005930", DAY, rows)
    bars_5m = resample(bars_1m, Resolution.M5)

    assert len(bars_5m) == 2
    first = bars_5m[0]
    assert first.ts.strftime("%H%M") == "0900"
    assert first.open == Decimal("100") and first.close == Decimal("104")
    assert first.high == Decimal("104") and first.volume == Decimal("50")
    assert bars_5m[1].open == Decimal("105") and bars_5m[1].close == Decimal("109")
