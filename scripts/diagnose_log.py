"""serve.log 일별 안정성 지표 — 수정 전후를 같은 잣대로 비교하기 위한 계측.

주장 대신 숫자로 확인하려고 만든다. 재접속·DNS 실패·API 오류를 날짜별로 뽑아
"고쳤다" 를 다음 거래일 수치로 검증한다.

    .venv/bin/python scripts/diagnose_log.py [--days 7]
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
TS = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})")
URL = re.compile(r"for url '([^?']+)")
EXC = re.compile(r"^([a-zA-Z_.]*(?:Error|Exception)[a-zA-Z]*)")

# 이벤트 → 라벨. serve.log 의 구조화 로그 키 기준.
EVENTS = {
    "feed.reconnect": "시세 재접속",
    "feed.error": "시세 오류",
    "process.error": "봉 처리 실패(격리)",
    "fill_poll.error": "체결폴링 실패",
    "scanner.joined": "스캐너 합류",
    "feed.stale": "시세 끊김 경보",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--log", default="logs/serve.log")
    args = ap.parse_args()

    text = ANSI.sub("", Path(args.log).read_text(encoding="utf-8", errors="replace"))
    lines = text.split("\n")

    per_day: dict[str, Counter[str]] = defaultdict(Counter)
    endpoints: dict[str, Counter[str]] = defaultdict(Counter)
    day = ""
    for line in lines:
        if m := TS.match(line):
            day = m.group(1)
            for key, label in EVENTS.items():
                if key in line:
                    per_day[day][label] += 1
        elif day:  # 트레이스백 본문 (직전 로그 항목에 귀속)
            if e := EXC.match(line):
                name = e.group(1)
                if "gaierror" in line or "gaierror" in name:
                    per_day[day]["DNS 실패"] += 1
                elif name.endswith("HTTPStatusError"):
                    per_day[day]["HTTP 오류"] += 1
                elif "ConnectError" in name:
                    per_day[day]["연결 실패"] += 1
            if "gaierror" in line:
                per_day[day]["DNS 실패"] += 1
            if u := URL.search(line):
                endpoints[day][u.group(1).split("/uapi")[-1]] += 1

    days = sorted(per_day)[-args.days :]
    if not days:
        print("집계할 로그가 없습니다.")
        return 0

    labels = ["시세 재접속", "시세 오류", "DNS 실패", "HTTP 오류", "봉 처리 실패(격리)",
              "체결폴링 실패", "스캐너 합류", "시세 끊김 경보"]
    width = max(len(x) for x in labels) + 2
    print(f"{'지표':<{width}}" + "".join(f"{d[5:]:>9}" for d in days))
    print("-" * (width + 9 * len(days)))
    for label in labels:
        row = "".join(f"{per_day[d].get(label, 0):>9}" for d in days)
        print(f"{label:<{width}}{row}")

    last = days[-1]
    if endpoints[last]:
        print(f"\n{last} 오류가 난 API (상위 3):")
        for path, n in endpoints[last].most_common(3):
            print(f"  {n:>5}x {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
