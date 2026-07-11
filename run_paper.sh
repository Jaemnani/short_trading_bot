#!/usr/bin/env bash
# 모의투자 가동: 멀티 전략 엔진 + 대시보드 API.
# 본인 터미널에서 실행하세요 (개발 세션과 독립적으로 살아있게).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs

if [ -f logs/serve.pid ] && ps -p "$(cat logs/serve.pid)" >/dev/null 2>&1; then
  echo "이미 가동 중입니다 (engine PID $(cat logs/serve.pid)). 먼저 ./stop_paper.sh"
  exit 1
fi

nohup .venv/bin/trader serve --config watchlist.json > logs/serve.log 2>&1 &
echo $! > logs/serve.pid
nohup .venv/bin/trader api > logs/api.log 2>&1 &
echo $! > logs/api.pid

sleep 3
echo "── 가동 완료 ─────────────────────────────"
echo "engine PID $(cat logs/serve.pid) → logs/serve.log"
echo "api    PID $(cat logs/api.pid)  → http://localhost:8000 (PWA: cd frontend && npm run dev)"
echo "중지: ./stop_paper.sh   |   긴급중지(전량청산)는 대시보드에서"
tail -5 logs/serve.log
