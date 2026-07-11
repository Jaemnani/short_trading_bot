#!/usr/bin/env bash
# 모의투자 중지 (엔진 + API). 보유 포지션 청산은 하지 않음 — 청산은 대시보드 긴급중지로.
cd "$(dirname "$0")"
for name in serve api; do
  if [ -f "logs/$name.pid" ]; then
    pid=$(cat "logs/$name.pid")
    kill "$pid" 2>/dev/null && echo "$name (PID $pid) 중지" || echo "$name: 이미 종료됨"
    rm -f "logs/$name.pid"
  fi
done
