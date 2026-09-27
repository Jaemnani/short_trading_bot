# 인계 문서 (2026-09-27 세션 → 다음 세션, Mitra 실험 준비 갱신)

> 새 채팅은 이 파일부터 읽는다. 규칙 원문은 `CLAUDE.md` → `PRINCIPLES.md`, 판정 기록은 `BACKLOG.md`/`STRATEGIES.md`.

## 1. 한 줄 요약
- 취약점 전수 점검(이슈 #1~#23)은 **PR #24로 main 머지 완료** (`ae5e1f5`, Codex 크로스리뷰 9회, 테스트 458 통과).
- 새 전략 도구 검토 결과: **Jev 기각**, **Mitra-v2 단독으로 실험 진행**.
- **Mitra-v2: 가설 H-M1 사전등록 완료(`BACKLOG.md`) + 실험 스크립트 `scripts/mitra_filter_experiment.py` 작성·검증 완료. 실데이터 실행은 아직 — iMac에서 돌려야 함 (§8).**

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

## 8. Mitra-v2 진행 현황 (09-27 두 번째 세션)
- ✅ §7-1 가설 사전등록: `BACKLOG.md` "가설 사전등록 H-M1" 절 (커밋 `324d1bb`, 스크립트보다 먼저). 실행 후 수정 금지 — 바꿀 일이 생기면 H-M2.
- ✅ §7-2 스크립트: `scripts/mitra_filter_experiment.py` (`fetch`→`predict`→`latency`→`evaluate`, 사용법은 파일 docstring). 봇 본체 무변경.
  - 테스트 `tests/test_mitra_experiment.py` 14종: 지지 집합 라벨 확정일 < 블록 시작일(엄격)·최근 5,000행 캡·지지 부족 시 통과·이어쓰기, 빈 게이트 = 운용 눌림목과 거래 동일, 셋업 표본이 진입일을 전부 덮음, 미래 봉을 바꿔도 과거 피처 불변, AUC·롤링·G1/G2 채점 규칙, Mitra 어댑터 배관(autogluon 있을 때만).
  - 합성 데이터로 `predict --model logit` → `evaluate` 전 과정 스모크 통과 (수치는 무의미).
- ⛔ 이 클라우드 세션에선 실행 불가: egress 정책이 `huggingface.co`(가중치)·네이버/KRX/야후(FDR 시세)를 403 차단. 환경 설정에서 허용하거나 iMac에서 실행.
- ⚠️ **지연 실측(클라우드 4코어 CPU, 무작위 가중치 동일 구조 76M)**: 지지 5,000행 1회 예측 **빠른 경로 68초** / 공개 API 132초. G3 기준(≤60초) 경계선.
  - 워크포워드 ~540블록 × 68초 ≈ **N 하나당 ~10시간(CPU)**, 사전등록상 Mitra 실행 4회(A×N5·10·20 + B×N10) ≈ 40시간. **MPS가 빨라야 현실적** → iMac에서 `latency` 먼저 돌려 총 시간 추정.
  - 느려도 블록 주기(5일)·지지 크기(5,000)는 사전등록 고정값 — 바꾸려면 H-M2로 재등록.
  - 같은 입력에서도 Mitra 출력이 실행마다 ±0.03~0.04 흔들림(bf16·지지 순열, 무작위 가중치 기준). 예측은 CSV로 캐시되므로 `evaluate`는 재현 가능.
- **다음 할 일 (iMac)**: docstring 순서대로 `fetch`(+`--delisted`는 오래 걸림, 밤에) → logit A/B → `latency` → mitra A(N 5·10·20)/B(N10) → `evaluate` → `data/mitra/report.md` 판정을 `BACKLOG.md`/`STRATEGIES.md`에 기록.
  - `fetch`의 상폐 종목 조회(`KRX-DELISTING:` 접두 폴백)는 네트워크 차단으로 미실측 — 실패 목록이 출력되니 확인.
  - AutoGluon 1.6.3 기본값은 v1(`mitra-classifier`). v2(`mitra-classifier-2`) 체크포인트가 1.6.3 `Tab2D.from_pretrained`와 호환되는지 미확인 — 안 되면 AutoGluon 업그레이드 후 `--slow`로 먼저 확인.
