# 운영 런북 — 실전 전환 상세 가이드 (P11+)

코드(P0–P11)는 완성·테스트 완료. 이 문서는 **실제 자금**까지 가는 남은 단계의 상세 절차다.
순서: **백테스트 → 모의투자(paper) → 실전 최소사이즈 → 점진 확대**. paper는 절대 건너뛰지 말 것.

---

## 0. 검증·적용 완료된 코드 수정 (공식 KIS 샘플 레포 대조, high confidence)

go-live 리서치에서 발견한 **실전 거부/오작동 유발 버그를 이미 수정**했다:

| 항목 | 이전(틀림) | 수정(검증) |
|---|---|---|
| 국내 매수 TR | TTTC0802U | **TTTC0012U** (모의 VTTC0012U) |
| 국내 매도 TR | TTTC0801U | **TTTC0011U** (+ `SLL_TYPE`) |
| 국내 정정/취소 TR | TTTC0803U(추측) | **TTTC0013U** |
| 차세대 주문 바디 | 없음 | **`EXCG_ID_DVSN_CD="KRX"`** (KRX/NXT/SOR) 추가 |
| 해외(미국) 취소 | 미구현 | **TTTT1004U** 구현 |
| 실시간 체결량 인덱스 | 13(ACML_VOL 누적) | **12 (CNTG_VOL 체결량)** |
| 증권거래세 | 0.18% | **0.20%** (2026 KOSPI·KOSDAQ) |

> ⚠️ 비미국 해외(홍콩/중국/일본/베트남) **정정·취소 TR_ID는 미검증** → `MarketRouter.cancel_tr_id`가 ValueError를 던짐. 해당 시장 거래 전 포털에서 확인 후 `_OVERSEAS_CANCEL`에 추가.

---

## A. 실전 전 반드시 구현해야 할 코드 배선(wiring) — 현재 미연동

엔진 프레임워크는 완성됐지만, 실거래 루프를 돌리려면 아래를 연결해야 한다(키 유무와 무관):

1. **관심종목(watchlist)**: `dict[str, StrategyTemplate]`(종목→전략/파라미터/해상도)을 만들어 `build_trading_service`/`TradingService`에 전달. 기본값 없음.
2. **피드 연결**: `KisWebSocketFeed(approval_key, tickers, resolution, ws_url=kis_ws_url(mode))`를 만들어 `TradingService.run(feed)`에 주입. (빌더/`serve`에 아직 미연결)
3. **approval_key**: 시작 시 `POST /oauth2/Approval`로 발급해 피드에 주입. WS 제약: 커넥션당 등록 ~41건, **동시 연결 1개** → 체결가/체결통보 채널 경합 설계 필요.
4. **체결 수신**: 국내 `H0STCNI0` / 해외 `H0GSCNI0` 체결통보 WS 구독 → 파싱 → `broker.fill_handler` 호출. **현재 미연동**(없으면 엔진이 체결을 모름).
5. **재접속/누락체결 복구**: WS 재접속마다 REST로 미체결·잔고 재조회해 누락 체결 보정. `fill.unknown_order`/`fill.oversell` 경고 0 확인.
6. **재시작 시 포지션 복원**: 시작 시 `Position` 테이블 조회 → `PositionLot` 복원 → `TradingService._lots` 동기화 후 `run()`. 미구현(없으면 재시작 시 보유 망각 → reconcile drift).
7. **RiskLimits 명시**: 빌더 기본은 `RiskLimits()`(전부 None=무제한). 실전은 반드시 `RiskLimits(daily_loss_limit, max_open_positions, max_order_notional, max_ticker_exposure)`(KRW 환산) 전달. `daily_loss_limit>0` 없으면 LIVE preflight 실패.
8. **뉴스(선택)**: 쓸 경우 `DartFetch`(OpenDartReader/httpx→list.json), `RssFetch`(feedparser/httpx) 콜러블 주입. 안 쓰면 `STB_DART_API_KEY` 비우고 생략.

---

## 1. KIS 계좌 + API 온보딩

1. **실계좌 개설**: 한국투자 앱/영업점에서 위탁(종합)계좌. 계좌번호 = 8자리 CANO + 2자리 ACNT_PRDT_CD(보통 `01`).
2. **모의투자 신청**: 한투 사이트 [트레이딩]>[모의투자]>[주식/선물옵션 모의투자]. 모의 REST `https://openapivts.koreainvestment.com:29443`, WS `ws://ops.koreainvestment.com:31000` (코드 `app/engine.py`와 일치).
3. **API 신청**: apiportal.koreainvestment.com 로그인 > [KIS Developers] > 오픈API 이용신청. **실전·모의 계좌를 각각 등록**. **이용기간 1년**(만료 전 갱신 — 지금 리마인더 설정).
4. **키 발급**: 계좌별 App Key/App Secret(실전·모의 상이) → `.env`의 `STB_KIS__LIVE__*`, `STB_KIS__PAPER__*`.
5. **토큰 2종 이해**: REST=OAuth Bearer(`/oauth2/tokenP`, `infra/kis_auth.py`가 처리) / WS=**별도 approval_key**(`/oauth2/Approval`) — 직접 발급해 피드에 전달.
6. **실시간 시세**: 국내(H0STCNT0 등)는 추가요금 없이 approval_key로 구독. 미국 등 해외는 HTS/MTS [해외주식]>거래신청>실시간 시세 신청(무료=유료의 ~50% 깊이, 유료 신청 시 무료 중단).
7. **해외 매매 시 통합증거금**: MTS [해외주식]>거래신청>통합증거금 서비스 신청 → 원화로 해외 매수(결제일 자동환전). **on-demand 환전 REST 없음** → 환전 시점/환율 코드 제어 불가, 외화 마이너스 잔고를 리스크 로직에서 처리.

---

## 2. `.env` + RiskLimits + Postgres/Alembic

- `.env`(루트, 커밋 금지) 필수: `STB_MODE`(처음 PAPER), `STB_KIS__LIVE__APP_KEY/APP_SECRET/ACCOUNT_NO`, `STB_API_JWT_SECRET`(기본값 금지, 32바이트+). 모의용 `STB_KIS__PAPER__*`도.
- 안전/운영: `STB_DRY_RUN=true`(시작), `STB_LOG_FORMAT=json`(prod), `STB_API_USERNAME/PASSWORD`(admin/admin 변경).
  - `STB_DRY_RUN=true` 이면 `STB_MODE=LIVE` 에서 `serve --live-exec`(실전 실주문)이 **기동 거부**된다. 모의계좌 `--live-exec` 에는 영향 없음. LIVE + `--live-exec` 는 preflight critical 전부 통과도 강제.
  - 대시보드: JWT 시크릿/비밀번호가 기본값이면 `trader api` 는 `--host 127.0.0.1` 로만 뜬다 (0.0.0.0 거부). LIVE preflight 에서도 `api_credentials_secure` 가 critical. 로그인 실패 10회/5분 → 429. CORS 는 기본 꺼짐(`STB_API_CORS_ORIGINS` 명시 목록만). `/ws` 는 `?token=<JWT>` 필요.
- 긴급중지 영속: 엔진이 STOP 을 받으면 즉시 `data/kill_switch.active` 를 만든다. 청산 도중 죽어도 재기동 시 STOPPED+전량청산을 이어서 하고, 보유·걸린 주문이 0 이 될 때까지 종료하지 않는다. 해제 = 대시보드 `resume` 또는 수동 `./run_paper.sh`.
- 의존성 고정: `requirements.lock`(해시 포함, `uv pip compile pyproject.toml --universal --generate-hashes` 로 갱신). 재현 설치는 `pip install --require-hashes -r requirements.lock && pip install --no-deps -e .`.
- 확인: `trader config` (시크릿 마스킹된 설정 출력).
- **PostgreSQL**: `STB_DB_URL=postgresql+asyncpg://user:pass@host/db`. 실전에 sqlite/`:memory:` 금지(멱등·reconcile가 재시작을 견뎌야 함; preflight `db_persistent` 체크).
- 마이그레이션: `alembic upgrade head` (prod는 `trader initdb` 쓰지 말 것 — dev 전용).
- **RiskLimits 명시 생성**(위 A-7).

---

## 3. 코드 배선 (위 A절을 실제로 연결)

watchlist 구성 → approval_key 발급 → `KisWebSocketFeed` 주입 → 체결통보 WS 수신→`fill_handler` → 재접속 복구 + 재시작 포지션 복원 → (선택) 뉴스 fetch 콜러블. 각 항목 A절 참조.

---

## 4. 실전 전 포털 재확인 항목

- 정정된 국내 TR(TTTC0012U/0011U/0013U) + `EXCG_ID_DVSN_CD`/`SLL_TYPE` 바디 필드.
- 잔고 응답 필드명(국내 hldg_qty/pdno/pchs_avg_pric/dnca_tot_amt, 해외 ovrs_pdno/ovrs_cblc_qty/frcr_dncl_amt1).
- H0STCNT0 레이아웃(time=1, price=2, **CNTG_VOL=12**).
- 사용할 해외 시장의 정정·취소 TR(미국 TTTT1004U 검증; HK/CN/JP/VN 미검증).
- **레이트리밋**: 실전 ~20/s, 모의 ~5/s(초과 시 EGW00201). **2026-03-20 신규고객 초당 호출 정책 공지**의 정확한 수치는 포털 로그인 확인 필요(현재 LOW confidence).
- 증권거래세(2026 KOSPI/KOSDAQ 0.20% 매도) 변동 여부.
- WS 한도(커넥션당 ~41 등록, 동시 1연결) + (해외 시) 무료/유료 실시간 범위.

---

## 5. 백테스트 + 워크포워드

- **비용모델 먼저 갱신**(완료: sell_tax 0.20%; fee 1.5bps/측, slippage 5bps 확인). 낡은 비용 위 엣지는 허구.
- 전략별 인샘플 백테스트: CAGR/MDD/Sharpe/승률/손익비 기록(손실 0이면 profit_factor=None 처리).
- **워크포워드**: 롤링 인/아웃샘플 — OOS에서 무너지는 전략 기각.
- 비용 민감도: 슬리피지 2배·세금 0.20%로 재실행해 엣지 소멸 지점 파악(고회전 단기전략이 취약).
- 백테스트 한계: ±30% 제한·상하한가 락업·VI·T+2 미모델링 → paper에서 검증.

---

## 6. 모의투자(paper) 검증 — 수 주간

`STB_MODE=PAPER, STB_DRY_RUN=false`, 모의 키, Postgres. `trader preflight` → `trader serve` + `trader api`.
**paper는 배관(plumbing) 검증이지 엣지 증명이 아님**(모의 체결은 비현실적으로 관대).

- **피드 지연**: 틱 도착시각 vs 거래소 시간 → p50/p95/p99 세션별 로깅. 09:00 개장·15:20~15:30 종가 단일가에서도 안정 확인.
- **피드 완전성**: 틱→봉 OHLCV가 백테스트와 일치(누락/중복 틱이 유령봉 → 지표 오염).
- **구독 한도**: watchlist가 ~41건에 맞는지, 캠페인 진입/청산 시 재등록 정상.
- **슬리피지 vs 모델**: 체결가·결정가·슬리피지(bps) 저장·집계(모의는 비현실적으로 낮음 — 실제 검증은 실전 최소사이즈).
- **멱등성**: 같은 client_order_id 2회 → 주문 1건; persist와 broker 호출 사이 프로세스 강제종료 후 재시작 → 정확히 1주문, audit_log `order.pending→order.accepted`.
- **재접속 reconcile**: WS 강제 단절·재시작 → 시작 시 + 재개 전 reconcile, in_sync 확인. **현 Reconciler는 탐지만**(자동복구 없음) → drift 시 매매 차단 게이트 확인.
- **누락 체결**: 재접속마다 미체결·잔고 재조회로 보정. `fill.unknown_order`/`fill.oversell` 0.
- **현금/결제**: 주문가능금액 vs 출금가능금액(D+2) 구분 사이징을 매도→익일 사이클로 확인.
- **캠페인**: 기간 캠페인 1회 완주 → 종료 시 전량청산·정산 P&L = Σ체결 − 수수료 − 세금.
- **알림/킬스위치**: 모든 경로(일일손실/최대포지션/익스포저/주문금액/EGW00201/WS단절/reconcile drift) 발화 + **모바일/웹 PWA 긴급중지** flat-all 확인.
- **레이트리밋 스로틀**: 버스트 시 5/s(모의) 이하 큐잉/백오프(EGW00201 없이).
- **캘린더/세션**: KST·KRX 휴장/반장·동시호가(08:30–09:00, 15:20–15:30) 회피. 휴일 경계 포함 실행.
- **거부 처리**: 모든 거부(레이트/현금부족/호가단위·제한폭/정지·VI/장마감) 무크래시·무재시도폭주, 유령수량 없음.

---

## 7. 실전 전환 — 최소사이즈 후 점진 확대

1. **DRY_RUN 프리플라이트**: `STB_MODE=LIVE, STB_DRY_RUN=true`, 실전 키 → `trader preflight`(critical 전부 OK). 실전 토큰·approval_key 발급, 실전 base URL(openapi:9443 / WS ops:21000)·실전 TR 전환 확인.
2. **설정 플립**: `STB_MODE=LIVE, STB_DRY_RUN=false`. RiskLimits·스로틀(20/s) 무장. Postgres만.
3. **최소사이즈**: 유동성 큰 대형주 1~2종, **1주(또는 최소금액)**. 세션1 목표는 수익이 아니라 라운드트립(submit→접수→체결통보→체결반영→잔고일치, EGW00201·drift 0).
4. **첫 2~3세션 상주**(킬스위치 열고): 실전 vs paper 패리티 — 체결가·큐·부분체결·실슬리피지·거부코드·09:00/15:20 WS 안정성.
5. **reconcile 게이트(실전)**: 모든 갭(단절/재시작/배포) 후 실전 잔고 reconcile, 불일치 시 차단. 첫 수회는 HTS/MTS 잔고화면과 육안 대조.
6. **점진 확대**: 여러 클린 세션(슬리피지 모델 내, drift 0, 알림 정상) 후 — 주문금액↑ → 동시 포지션수↑ → watchlist↑, **각 단계마다 슬리피지·거부율 재검증**(사이즈↑=호가 더 먹음, 종목↑=req/s가 20/s 천장 접근).
7. **롤백 플랜**: 일일손실 도달/설명불가 drift/슬리피지 초과/거부율 급증 → **긴급중지(flat-all)** → `DRY_RUN=true` → 최소사이즈/paper 복귀. 킬스위치·일일손실한도는 **전 사이즈 영구 무장**.

---

## 8. 상시 안전 + KR 시장 함정 + 법무/세금

**코드로 강제할 KR 함정**:
- **±30% 가격제한폭**: 전일종가 기준 밴드 밖 주문은 KRX 거부 → 클램프/거부, 전일종가로 밴드 계산.
- **상한가/하한가 락업**: 밴드에서 체결 보장 없음(롱 봇은 하한가서 매도 불가) → no-fill로 모델, 대기 주문 피라미딩 금지.
- **VI(변동성완화)**: 정적 ±10% / 동적 ±3%(KOSPI200)·±6%(일반·KOSDAQ) → 2분 단일가. 상태 감지해 해당 종목 로직 일시정지.
- **서킷브레이커** 8/15/20% + **사이드카** ±5% → 시장 전체 halt 감지, 제출 중단, 재개 시 reconcile.
- **변동 호가단위(호가단위)**: 가격대별 틱(예: 5만~20만원=100원). 모든 지정가/타깃/손절가를 틱에 반올림(아니면 거부).
- **동시호가** 창에서는 연속체결 로직 비활성(일괄·랜덤엔드).
- **T+2/D+2**: 주문가능금액 vs 출금가능금액 구분 사이징.

**모니터링 대시보드**(`trader api` + HTTPS 리버스프록시): 순수익(수수료·세금 차감)·MDD(알림)·Sharpe vs 백테스트·승률·손익비·**실현 슬리피지 bps vs CostModel(실전 핵심 지표)**·라운드트립 비용·거부율(사유별)·체결/부분체결율·WS 수신지연 p50/95/99·주문 왕복지연·**reconcile drift(목표 0)**·WS 가동률/단절/누락체결·실전-paper 패리티·매수여력 vs 정산현금·종목/총 KRW 익스포저 vs 한도·스로틀 사용률(peak req/s vs 20).

**법무/세금**: 증권거래세 매도 0.20%(코드 반영 완료) — 순손익 반영. 해외 FX는 통합증거금 자동환전(코드 제어 불가) — 실현 FX 추적·외화 마이너스 처리. **자동매매/세무 신고 의무는 KIS·세무 전문가에 사전 확인** 후 실자금 확대.
