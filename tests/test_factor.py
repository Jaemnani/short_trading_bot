"""팩터(저PBR+흑자) 선별 — fundamentals 모듈 tests."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from short_trading_bot.market.fundamentals import (
    DartFundamentals,
    FundamentalRow,
    applicable_fiscal_year,
    parse_single_account,
    select_factor_picks,
)


def _row(
    ticker: str,
    *,
    marcap: float,
    equity: float,
    income: float,
    close: float = 10_000,
) -> FundamentalRow:
    return FundamentalRow(
        ticker=ticker, name=f"종목{ticker}", marcap=Decimal(str(marcap)),
        close=Decimal(str(close)), equity=Decimal(str(equity)),
        net_income=Decimal(str(income)), fiscal_year=2025,
    )


# -- 선견 차단: Y년 사업보고서는 Y+1년 7월부터 ---------------------------------


def test_fiscal_year_after_july_uses_last_year() -> None:
    assert applicable_fiscal_year(date(2026, 8, 3)) == 2025
    assert applicable_fiscal_year(date(2026, 7, 1)) == 2025


def test_fiscal_year_before_july_uses_two_years_ago() -> None:
    assert applicable_fiscal_year(date(2026, 6, 30)) == 2024
    assert applicable_fiscal_year(date(2026, 1, 2)) == 2024


# -- 순수 선별 -----------------------------------------------------------------


def test_selects_low_pbr_profitable_equal_weight() -> None:
    rows = [
        _row("A", marcap=100, equity=200, income=10),  # PBR 0.5
        _row("B", marcap=100, equity=50, income=10),  # PBR 2.0
        _row("C", marcap=100, equity=100, income=10),  # PBR 1.0
    ]
    picks = select_factor_picks(rows, top=2)
    assert [p.ticker for p in picks] == ["A", "C"]
    assert picks[0].pbr == 0.5
    assert all(p.weight == 0.5 for p in picks)


def test_excludes_loss_makers_and_negative_equity() -> None:
    rows = [
        _row("적자", marcap=100, equity=500, income=-1),  # 최저 PBR이지만 적자
        _row("자본잠식", marcap=100, equity=-10, income=10),
        _row("정상", marcap=100, equity=100, income=10),
    ]
    picks = select_factor_picks(rows, top=10)
    assert [p.ticker for p in picks] == ["정상"]
    assert picks[0].weight == 1.0


def test_empty_when_no_eligible() -> None:
    assert select_factor_picks([_row("A", marcap=100, equity=100, income=0)]) == []


# -- fnlttSinglAcnt 파싱 -------------------------------------------------------


def _payload(rows: list[dict[str, str]], status: str = "000") -> bytes:
    return json.dumps({"status": status, "message": "정상", "list": rows}).encode()


def test_parse_prefers_cfs_and_partial_income_match() -> None:
    raw = _payload([
        {"fs_div": "OFS", "account_nm": "자본총계", "thstrm_amount": "1,000"},
        {"fs_div": "OFS", "account_nm": "당기순이익", "thstrm_amount": "10"},
        {"fs_div": "CFS", "account_nm": "자본총계", "thstrm_amount": "2,000"},
        # 부분일치 필요 사례: "당기순이익(손실)" 표기 (정확일치는 전멸 — 검증 실측)
        {"fs_div": "CFS", "account_nm": "당기순이익(손실)", "thstrm_amount": "-3,500"},
    ])
    parsed = parse_single_account(raw)
    assert parsed == (Decimal("2000"), Decimal("-3500"))


def test_parse_falls_back_to_ofs_when_no_cfs() -> None:
    raw = _payload([
        {"fs_div": "OFS", "account_nm": "자본총계", "thstrm_amount": "1,000"},
        {"fs_div": "OFS", "account_nm": "당기순이익", "thstrm_amount": "10"},
    ])
    assert parse_single_account(raw) == (Decimal("1000"), Decimal("10"))


def test_parse_none_on_error_status_or_missing_accounts() -> None:
    assert parse_single_account(_payload([], status="013")) is None  # 조회 데이터 없음
    only_equity = _payload([
        {"fs_div": "CFS", "account_nm": "자본총계", "thstrm_amount": "1,000"},
    ])
    assert parse_single_account(only_equity) is None


# -- DartFundamentals (fake transport) -----------------------------------------


@pytest.mark.asyncio
async def test_fetch_uses_cached_corp_map(tmp_path: Path) -> None:
    (tmp_path / "dart_corp_codes.json").write_text(json.dumps({"005930": "00126380"}))
    calls: list[tuple[str, dict[str, str]]] = []

    async def transport(url: str, params: dict[str, str]) -> bytes:
        calls.append((url, params))
        return _payload([
            {"fs_div": "CFS", "account_nm": "자본총계", "thstrm_amount": "3,000"},
            {"fs_div": "CFS", "account_nm": "당기순이익", "thstrm_amount": "300"},
        ])

    dart = DartFundamentals("key", cache_dir=tmp_path, transport=transport)
    assert await dart.fetch("005930", 2025) == (Decimal("3000"), Decimal("300"))
    assert calls[0][1]["corp_code"] == "00126380"
    assert calls[0][1]["bsns_year"] == "2025"
    assert calls[0][1]["reprt_code"] == "11011"
    # corp map에 없는 종목(우선주 등)은 API 호출 없이 None
    assert await dart.fetch("005935", 2025) is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fetch_swallows_transport_errors(tmp_path: Path) -> None:
    (tmp_path / "dart_corp_codes.json").write_text(json.dumps({"005930": "00126380"}))

    async def broken(url: str, params: dict[str, str]) -> bytes:
        raise OSError("network down")

    dart = DartFundamentals("key", cache_dir=tmp_path, transport=broken)
    assert await dart.fetch("005930", 2025) is None
