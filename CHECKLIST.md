# 내가 할 일 체크리스트 (실전까지)

> 짧은 실행용 목록. 자세한 설명은 `OPERATIONS.md`.
> 코드는 다 됐고(테스트 162개 통과), 아래만 하면 됩니다. 순서대로.

## 1단계 · 계좌·키 발급 (반나절)
- [ ] 한국투자증권 **실계좌** 개설 (한투 앱/영업점)
- [ ] **모의투자** 신청 — 한투 [트레이딩]>[모의투자]>[주식 모의투자]
- [ ] apiportal.koreainvestment.com 로그인 → **API 신청**(실전·모의 둘 다 등록) → **appkey/appsecret** 발급
      *(이용기간 1년 — 만료 전 갱신 알림 설정)*
- [ ] (해외주식 할 거면) MTS [해외주식]>거래신청 → **실시간 시세** + **통합증거금** 신청

## 2단계 · 설정 (.env 채우기)
- [ ] 루트에 `.env` 생성(`.env.example` 복사). 채울 값:
  - [ ] `STB_MODE=PAPER`  ·  `STB_DRY_RUN=true`
  - [ ] `STB_KIS__PAPER__APP_KEY / APP_SECRET / ACCOUNT_NO`  (모의 키)
  - [ ] `STB_KIS__LIVE__APP_KEY / APP_SECRET / ACCOUNT_NO`    (실전 키)
  - [ ] `STB_API_JWT_SECRET` = 랜덤 32자 이상  ·  `STB_API_USERNAME / PASSWORD` (admin/admin 변경)
  - [ ] `STB_DB_URL` = PostgreSQL 주소 (예: `postgresql+asyncpg://user:pass@localhost/stb`)
- [ ] 확인: `.venv/bin/trader config`

## 3단계 · 코드 배선 (개발 — 직접 하거나 나에게 요청)
> 이건 클릭으로 안 되고 코드 연결이 필요합니다. "이거 해줘" 하면 됩니다.
- [ ] 관심종목(watchlist) + 위험한도(RiskLimits) 지정
- [ ] 실시간 피드 연결 + approval_key 발급 + **체결통보 수신**(H0STCNI0/해외 H0GSCNI0)
- [ ] 재시작 시 보유포지션 복원
- [ ] `alembic upgrade head` 로 DB 테이블 생성

## 4단계 · 모의투자 검증 (몇 주)
- [ ] `.venv/bin/trader preflight`  (체크 통과 확인)
- [ ] `.venv/bin/trader serve`  (엔진)  +  `.venv/bin/trader api`  (대시보드)
- [ ] 대시보드(모바일/웹)에서 포지션·손익 보이고 **일시중지/긴급중지** 동작 확인
- [ ] 며칠~몇 주 돌리며 체결·재접속·알림 정상 확인 *(모의 체결은 후하니 수익은 신뢰 X, "잘 돌아가는지"만)*

## 5단계 · 실전 (최소 사이즈부터)
- [ ] `.env`: `STB_MODE=LIVE`, `STB_DRY_RUN=true` → `trader preflight` 전부 OK 확인
- [ ] `STB_DRY_RUN=false` 로 전환
- [ ] **1주(최소 금액)·대형주 1~2종**으로 시작, 첫 2~3일은 옆에서 지켜보기(킬스위치 열어두고)
- [ ] 실전이 모의와 같게 나오면 → 금액 → 종목수 **천천히** 늘리기
- [ ] 문제 생기면 **긴급중지(전량청산)** → `STB_DRY_RUN=true` 복귀

## 항상 지킬 것
- [ ] 일일 손실한도 + 킬스위치 **항상 켜두기**
- [ ] 대시보드는 HTTPS 뒤에서만 공개, `.env`·키 절대 커밋 금지
- [ ] 자동매매/세금 신고 의무는 KIS·세무사에 확인 후 자금 확대
