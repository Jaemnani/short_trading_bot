# short_trading_bot

한국투자증권(KIS) API 기반 자동매매 봇. 자세한 내용:
- **PRINCIPLES.md** — 개발 원칙 매뉴얼 (전략 개발·검증 전 반드시 확인. 특히 백테스팅 금기 4종)
- STRATEGIES.md — 운용 전략·기각 사유·변경 히스토리
- BACKLOG.md — 개선 대기 목록과 판정 기록
- OPERATIONS.md — 운영 런북

## 개발 규칙 (요약 — 전체는 PRINCIPLES.md)
- 검증에서 이긴 것만 적용. 기존 검증된 동작은 기본값으로 보존 (새 장치는 기본 꺼짐).
- 워크포워드(선견편향 차단) + 하락장 분리 평가 + 생존편향 대조 필수.
- 가설 먼저 문서화 → 스윕은 그 검증만. 단일 최고점 금지, 견고 클러스터만 채택.
- 검증·판정은 STRATEGIES.md/BACKLOG.md에 기록.
- 한국 시장에 집중 (미국 확장 보류 — 유저 지시).
- 테스트: `.venv/bin/python -m pytest tests` (전부 통과 유지), ruff + mypy --strict 클린 유지.
