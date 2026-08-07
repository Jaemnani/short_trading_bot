#!/usr/bin/env bash
# 모의투자 가동/워치독: tmux 세션 'stb' 에 엔진 + 대시보드 API.
# --live-exec = KIS 모의계좌 실주문 (2026-08-03 전환). 시뮬 체결로 되돌리려면 플래그 제거.
#
# 사용:
#   ./run_paper.sh              수동 가동 (중지 마커 해제 후 시작)
#   ./run_paper.sh --watchdog   launchd 용 (5분 주기) — 죽은 창만 되살리고,
#                               중지 마커(data/engine_stopped.marker)가 있으면 존중.
# 마커는 ./stop_paper.sh 와 엔진의 긴급중지 종료가 남긴다 — 의도된 중지를
# 워치독이 멋대로 되살리지 않게 하는 장치.
set -euo pipefail
cd "$(dirname "$0")"

command -v tmux >/dev/null 2>&1 || { echo "tmux가 필요합니다: brew install tmux"; exit 1; }
mkdir -p logs data

MARKER="data/engine_stopped.marker"
ENGINE_CMD="$(pwd)/.venv/bin/trader serve --config watchlist.json --live-exec 2>&1 | tee -a logs/serve.log"
API_CMD="$(pwd)/.venv/bin/trader api 2>&1 | tee -a logs/api.log"

if [ "${1:-}" = "--watchdog" ]; then
  [ -f "$MARKER" ] && exit 0   # 의도된 중지 — 되살리지 않음
else
  rm -f "$MARKER"              # 수동 가동 = 명시적 재개
fi

if ! tmux has-session -t stb 2>/dev/null; then
  tmux new-session -d -s stb -n engine "$ENGINE_CMD"
  tmux new-window -t stb -n api "$API_CMD"
  echo "── tmux 세션 'stb' 가동 ($(date '+%F %T')) ──────────────"
  echo "  실시간 보기 : tmux attach -t stb   /  대시보드: http://localhost:8000"
  echo "  중지        : ./stop_paper.sh   (긴급중지·전량청산은 대시보드에서)"
  exit 0
fi

# 세션은 있는데 창이 죽었으면 그 창만 되살린다 (2026-08-03 엔진만 죽고 나흘 방치 재발 방지)
windows=$(tmux list-windows -t stb -F '#{window_name}')
if ! grep -q '^engine$' <<<"$windows"; then
  tmux new-window -t stb -n engine "$ENGINE_CMD"
  echo "[watchdog] 엔진 창 사망 감지 → 재가동 ($(date '+%F %T'))"
fi
if ! grep -q '^api$' <<<"$windows"; then
  tmux new-window -t stb -n api "$API_CMD"
  echo "[watchdog] API 창 사망 감지 → 재가동 ($(date '+%F %T'))"
fi
if [ "${1:-}" != "--watchdog" ]; then
  echo "이미 가동 중입니다 → tmux attach -t stb  (중지: ./stop_paper.sh)"
fi
