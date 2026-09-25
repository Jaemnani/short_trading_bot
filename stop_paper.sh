#!/usr/bin/env bash
# 모의투자 중지 (tmux 세션 'stb' 종료). 보유 포지션 청산은 하지 않음 — 청산은 대시보드 긴급중지로.
cd "$(dirname "$0")"
mkdir -p data && touch data/engine_stopped.marker  # 워치독이 되살리지 않게 (재개: ./run_paper.sh)
tmux kill-session -t stb 2>/dev/null && echo "tmux 세션 'stb' 중지됨" || echo "가동 중인 세션 없음"
# 구버전(nohup) 잔여 프로세스 정리 — 이 체크아웃의 venv 로 띄운 것만.
# "trader serve" 만으로 찾으면 다른 디렉토리(예: 실전 체크아웃)의 엔진까지 포지션 보유 중 죽인다.
TRADER_RE="$(pwd | sed 's/[][\\.^$*+?(){}|/]/\\&/g')/\.venv/bin/trader"
pkill -f "${TRADER_RE} serve" 2>/dev/null || true
pkill -f "${TRADER_RE} api" 2>/dev/null || true
