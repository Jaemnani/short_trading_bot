"""KRX_HOLIDAYS 표를 실제 거래 데이터와 대조 — 표 갱신 시 반드시 실행.

위험 비대칭: 휴장일 누락은 무해(폴링 시도 후 백오프)지만, 거래일을 휴장으로 잘못 넣으면
그날 체결 폴링이 통째로 멈춘다. 그래서 "잘못 넣은 거래일 0" 이 통과 기준이다.

지나간 기간만 검증할 수 있다 (미래 날짜는 공표 달력을 직접 대조할 것).

    .venv/bin/python scripts/verify_holidays.py [연도]
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

import FinanceDataReader as fdr

from short_trading_bot.execution.poll_gate import KRX_HOLIDAYS


def main() -> int:
    year = int(sys.argv[1]) if len(sys.argv) > 1 else date.today().year
    today = date.today()
    start, end = date(year, 1, 1), min(date(year, 12, 31), today - timedelta(days=1))
    if start > end:
        print(f"{year}년은 아직 검증할 구간이 없습니다.")
        return 0

    traded = {d.date() for d in fdr.DataReader("KS11", str(start), str(end)).index}
    weekdays, cur = [], start
    while cur <= end:
        if cur.weekday() < 5:
            weekdays.append(cur)
        cur += timedelta(days=1)

    actual = set(weekdays) - traded  # 정답: 거래 없던 평일
    listed = {h for h in KRX_HOLIDAYS if start <= h <= end}
    missing = sorted(actual - listed)  # 무해
    wrong = sorted(listed - actual)  # 위험

    print(f"검증 구간: {start} ~ {end} (실제 거래일 {len(traded)}일)")
    print(f"빠뜨린 휴장일 ({len(missing)}) — 무해: {[str(d) for d in missing] or '없음'}")
    print(f"잘못 넣은 거래일 ({len(wrong)}) — 위험: {[str(d) for d in wrong] or '없음'}")
    if weekend := sorted(h for h in KRX_HOLIDAYS if h.weekday() >= 5):
        print(f"주말 항목 (불필요, 제거 권장): {[str(d) for d in weekend]}")
    if wrong:
        print("\n❌ 실패 — 거래일이 휴장으로 표기돼 그날 체결 폴링이 멈춥니다. 표를 고치세요.")
        return 1
    print("\n✅ 통과 — 거래일을 막는 항목 없음.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
