#!/usr/bin/env bash
# 모의투자 가동: tmux 세션 'stb' 에 엔진 + 대시보드 API.
# --live-exec = KIS 모의계좌에 실제 주문 (2026-08-03 전환). 시뮬 체결로 되돌리려면 플래그 제거.
# 보기: tmux attach -t stb   (창 전환 Ctrl-b n, 분리 Ctrl-b d)
set -euo pipefail
cd "$(dirname "$0")"

command -v tmux >/dev/null 2>&1 || { echo "tmux가 필요합니다: brew install tmux"; exit 1; }

if tmux has-session -t stb 2>/dev/null; then
  echo "이미 가동 중입니다 → tmux attach -t stb  (중지: ./stop_paper.sh)"
  exit 1
fi

mkdir -p logs
tmux new-session -d -s stb -n engine \
  "$(pwd)/.venv/bin/trader serve --config watchlist.json --live-exec 2>&1 | tee -a logs/serve.log"
tmux new-window -t stb -n api \
  "$(pwd)/.venv/bin/trader api 2>&1 | tee -a logs/api.log"

sleep 3
echo "── tmux 세션 'stb' 가동 완료 ──────────────────"
echo "  실시간 보기 : tmux attach -t stb   (engine/api 창 전환: Ctrl-b n, 나가기: Ctrl-b d)"
echo "  로그 파일   : logs/serve.log, logs/api.log"
echo "  대시보드    : http://localhost:8000"
echo "  중지        : ./stop_paper.sh   (긴급중지·전량청산은 대시보드에서)"
