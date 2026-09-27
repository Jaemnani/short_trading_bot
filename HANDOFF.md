# 인계 문서 (2026-09-27 세션 → 다음 세션)

> 새 채팅은 이 파일부터 읽는다. 규칙 원문은 `CLAUDE.md` → `PRINCIPLES.md`, 판정 기록은 `BACKLOG.md`/`STRATEGIES.md`.

## 1. 한 줄 요약
- 취약점 전수 점검(이슈 #1~#23)은 **PR #24로 main 머지 완료** (`ae5e1f5`, Codex 크로스리뷰 9회, 테스트 458 통과).
- 새 전략 도구 검토 결과: **Jev 기각**, **Mitra-v2 단독으로 실험 진행** (아직 코드 없음 — 가설 문서부터).

## 2. 작업 규칙 (요약 — 어기면 결과 무효)
- 검증에서 이긴 것만 적용. 새 장치는 **기본 꺼짐**, 기존 검증된 동작은 기본값 보존.
- 워크포워드(선견편향 차단) + 하락장(2018/2022/2026-07) 분리 평가 + 생존편향 대조 + 롤링 6·12개월 구간 분포 채점.
- **가설을 먼저 문서화** → 스윕은 그 검증만. 단일 최고점 금지, 견고 클러스터만 채택.
- 판정은 `STRATEGIES.md`/`BACKLOG.md`에 기록. 한국 시장 집중(미국 확장 보류).
- PRINCIPLES §5: "AI는 예측기가 아니라 리서치·구현·검증 에이전트 — 종목을 찍지 않는다". Mitra-v2도 **기존 전략의 필터/보조 신호** 형태로만 검토한다.
- 테스트 `.venv/bin/python -m pytest tests` 전부 통과, `ruff check .` + `mypy --strict` 클린 유지.

## 3. 환경 메모
- venv는 **Python 3.12 필수**: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"` (3.11은 설치 실패).
- 클라우드 세션 egress 프록시가 arxiv·docs 사이트 일부를 막는다 → WebSearch로 우회.
- GitHub은 MCP 도구(`mcp__github__*`)로. Codex 리뷰는 PR에 `@codex review` 코멘트 → `chatgpt-codex-connector` 봇이 리뷰.
- 작업 브랜치: `claude/scalping-bot-vulnerability-check-6vd47c` (머지된 뒤 후속 작업은 최신 main에서 같은 이름으로 재시작).

## 4. 보안 점검(PR #24)으로 바뀐 운영 포인트
- 대시보드 API: `STB_API_JWT_SECRET`(32자 이상)·`STB_API_PASSWORD`(기본값 금지)를 안 바꾸면 **loopback 외 주소로 기동 거부**. CORS는 `STB_API_CORS_ORIGINS` 명시 목록만, WS는 `?token=` JWT 필수, 로그인 실패 제한(5분 10회).
- LIVE `--live-exec`: `STB_DRY_RUN=true`면 기동 거부, preflight critical(`api_credentials_secure` 포함) 통과 필수.
- 킬스위치 영속: `data/kill_switch.active` 파일이 있으면 재시작해도 신규 매수 중단 유지. 전량청산 후 정상 종료 시 삭제.
- 재시작 복구: 미체결/당일 체결 재조회 → 랏 복원, 일일 손익·최고평가금 복원, 전일 미체결 만료, 체결 재조회 전 매도 금지 배리어.
- **남은 확인(실측 필요)**: KIS 포털에서 ① 잔고 `prvs_rcdl_excc_amt`(D+2 예수금) 값이 기대대로인지 ② `inquire-daily-ccld` 연속조회 `tr_cont`·과거일 조회 동작.

## 5. Jev 판정 (기각 — 재론 불필요)
- 이유 ①(결정적): **외부 유료 호스팅 API** (가중치 비공개, 웨이트리스트) — 유저 결정으로 제외.
- 이유 ②: awesome-jev 4개 목록의 트레이딩 프로젝트 32개 전수 검토 → **엣지 입증 0건**. 엄밀한 검증은 전부 음수
  (29.8만 건 적중률 48.37%·비용 전 -0.30bp / 홀드아웃 -15.73% vs 바이앤홀드 +25.55% / 벤치 비용 후 -33.9bp / 사전등록 테스트 패배).
- 유일한 강점은 텍스트 분류(공시 분류 88%) — 스캐너용 DART 필터는 이미 사후검증에서 기각(기본 꺼짐)이라 쓸 곳 없음.

## 6. Mitra-v2 — 알려진 사실
- Amazon 표 형식(tabular) 파운데이션 모델, 76.7M 파라미터, **Apache-2.0**, 로컬 실행 가능.
- **합성 데이터로만 사전학습** → 시장 과거 데이터 오염(선견) 없음. 학습 없이 in-context learning(지지 집합을 넣으면 바로 예측).
- 사전학습 컨텍스트 ≤5,120행/≤50피처, 추론 지지 집합 최대 32,768행.
- HF: `autogluon/mitra-classifier-2`, `autogluon/mitra-regressor-2`, `autogluon/mitra-finetune`. AutoGluon에 통합.
- 한계: iid 가정 모델 → 시계열에 쓰려면 **지지 집합을 엄격히 과거로만** 구성해야 함. 공개 지연 수치는 GPU(H100) 기준, **iMac CPU/MPS 지연은 미측정**.
- 의존성 무거움(torch/autogluon) → 본체 의존성에 넣지 말고 `[ml]` 같은 optional extra로 분리.

## 7. Mitra-v2 실험 계획 (다음 세션 할 일, 순서대로)
1. **가설 사전등록** (`BACKLOG.md` 가설 표에 먼저 기록):
   - 예) "눌림목 1D 진입 신호 중 Mitra-v2가 '다음 N봉 수익 > 왕복비용'일 확률이 낮다고 본 신호를 거르면, 워크포워드·하락장 분리·롤링 12개월 분포에서 현행보다 낫다."
   - 채택 기준을 숫자로 미리 고정 (예: 롤링 12개월 플러스 비율·중앙값·최악 구간이 모두 현행 이상, MDD 악화 없음).
2. **오프라인 실험 스크립트** (`scripts/` 아래, 봇 본체 무변경):
   - 라벨: 진입 후 N봉 수익을 비용 대비 3분류(이익/무의미/손실) 또는 이진.
   - 피처 ≤50개, 전부 해당 시점까지의 값만 (지표·거래량·레짐 등 기존 `market/indicators.py` 재사용).
   - 워크포워드: 예측 시점 t에서 지지 집합은 t 이전 데이터만. 라벨이 t 이후에 확정되는 표본은 제외(라벨 누수 차단).
   - 기존 백테스트 하네스(`backtest/harness.py`)와 같은 비용 모델(`backtest/costs.py`)로 비교.
3. **평가**: 하락장 분리, 생존편향 대조(상폐 목록), 롤링 구간 분포, 이웃 설정(N·임계값)까지 통과하는 견고 클러스터인지.
4. **iMac 지연 측정**: 1회 예측 시간(CPU/MPS) — 1D·60m 전략이면 여유, 1분봉 스캐너용은 측정 후 판단.
5. 이기면: `[ml]` extra + 전략 옵션(기본 꺼짐)으로 배선 → 모의투자 관찰 → 판정 기록. 지면: 기각 사유를 `BACKLOG.md`/`STRATEGIES.md`에 기록하고 종료.
