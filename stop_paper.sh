#!/usr/bin/env bash
# 모의투자 중지 (tmux 세션 'stb' 종료). 보유 포지션 청산은 하지 않음 — 청산은 대시보드 긴급중지로.
tmux kill-session -t stb 2>/dev/null && echo "tmux 세션 'stb' 중지됨" || echo "가동 중인 세션 없음"
# 구버전(nohup) 잔여 프로세스 정리
pkill -f "trader serve" 2>/dev/null || true
pkill -f "trader api" 2>/dev/null || true
