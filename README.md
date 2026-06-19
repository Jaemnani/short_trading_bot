# short_trading_bot

한국투자증권(KIS) Developers API 기반 **국내·해외 주식 자동매매 봇**.

- **롱 온리(long-only)** — "매도"는 보유 롱 청산. 매매 *방향*만 한정이며 *빈도*는 무관(틱~분~일봉 회전매매 가능).
- **PositionLot** = 구입마다 동적 생성되는 독립 "주식객체"(자체 상태머신 + 알고리즘으로 자율 매수/매도).
- **플러그블 알고리즘** — `@register_strategy`로 새 알고리즘을 추가해 포지션별로 옵션 선택.
- **멀티 해상도** — 틱 / 1·3·5·10·15·30·60분 / 일·주·월.
- **기간 한정 운용(Campaign)** — 시작 예산 매수 → 기간 종료 시 전량 청산 → 정확한 손익 정산.
- **모의 ↔ 실전 토글**, **국내 + 해외**(미국/홍콩/일본/중국/베트남) + 통합증거금 기반 환전.
- 멀티 디바이스(모바일/태블릿/웹 PWA) 모니터링 + 원격 일시중지/긴급중지.

상세 설계·로드맵: `~/.claude/plans/api-dreamy-matsumoto.md`

## 개발 환경

- Python **3.12** (3.14는 pandas-ta/TA-Lib/torch 미지원 가능 → 3.12 고정)
- 의존성: 현재 P0 코어만 설치(SQLAlchemy 2 async, pydantic, structlog, typer, httpx, websockets). 지표/백테스트/ML 라이브러리는 해당 단계에서 추가.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # KIS 키 등 입력 (절대 커밋 금지)
```

## 실행

```bash
.venv/bin/trader version        # 버전
.venv/bin/trader config         # 효과적 설정(시크릿 마스킹)
.venv/bin/alembic upgrade head  # DB 스키마 생성
.venv/bin/trader serve          # 엔진 실행 (P1+에서 오케스트레이터 구현)
```

## 검증

```bash
.venv/bin/pytest        # 단위 테스트
.venv/bin/ruff check .  # 린트
.venv/bin/mypy short_trading_bot  # 타입체크
```

## 구조 (요약)

```
short_trading_bot/
  domain/      # PositionLot, PositionParams, Campaign, enums(Mode/Resolution/Market/...)
  strategy/    # Strategy ABC + registry + algorithms/ 플러그인 + rules/ 빌딩블록
  market/      # feed, bar_builder(틱→분봉), indicators, data
  execution/   # order_manager(멱등), reconciler, fx, broker/(kis·kis_overseas·paper)
  risk/        # manager(게이트), limits, kill_switch, control(pause/stop), rate_limit
  portfolio/   # 멀티통화 → KRW 환산 통합 한도
  news/        # DART + RSS 폴러, 감성, EWMA 집계 (공식 소스만)
  persistence/ # SQLAlchemy 2 async models + Alembic
  infra/       # config, logging, kis_auth, event_bus, notifier
  app/         # service(asyncio 오케스트레이터), scheduler, campaign_manager, cli
  backtest/    # harness, costs ; reports/ ; api/(FastAPI) ; frontend/(React PWA)
```

> ⚠️ 상용/배포 용도 — KIS 약관·투자일임 규제·감성모델 라이선스·기사 저작권·외국환거래법은 라이브 전 검토 필요.
