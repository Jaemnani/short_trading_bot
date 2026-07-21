"""DartRiskChecker (위험공시 필터) tests — injected transport, offline."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date

from short_trading_bot.news.risk import DartRiskChecker


def _corp_zip() -> bytes:
    xml = (
        "<result>"
        "<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
        "<stock_code>005930</stock_code></list>"
        "<list><corp_code>99999999</corp_code><corp_name>비상장사</corp_name>"
        "<stock_code></stock_code></list>"
        "</result>"
    ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml)
    return buf.getvalue()


def _make(tmp_path, filings):
    calls = []

    async def transport(url: str, params: dict) -> bytes:
        calls.append(url)
        if url.endswith("corpCode.xml"):
            return _corp_zip()
        return json.dumps({"status": "000", "list": filings}).encode()

    return DartRiskChecker("k", cache_dir=tmp_path, transport=transport), calls


async def test_risky_filing_detected_and_corp_map_cached(tmp_path) -> None:
    checker, _calls = _make(tmp_path, [
        {"report_nm": "주요사항보고서(유상증자결정)"},
        {"report_nm": "분기보고서"},
    ])
    assert await checker.is_risky("005930", on=date(2026, 7, 1))
    # corp map은 디스크 캐시 → 새 인스턴스는 zip 다운로드 없이 동작
    checker2, calls2 = _make(tmp_path, [{"report_nm": "분기보고서"}])
    assert not await checker2.is_risky("005930", on=date(2026, 7, 1))
    assert not any(u.endswith("corpCode.xml") for u in calls2)


async def test_unknown_ticker_or_error_is_not_risky(tmp_path) -> None:
    checker, _ = _make(tmp_path, [{"report_nm": "유상증자결정"}])
    assert not await checker.is_risky("999999", on=date(2026, 7, 1))  # 매핑 없음 → 통과

    async def broken(url: str, params: dict) -> bytes:
        raise RuntimeError("network down")

    checker3 = DartRiskChecker("k", cache_dir=tmp_path / "x", transport=broken)
    assert not await checker3.is_risky("005930")  # 조회 실패가 매매를 막지 않는다
