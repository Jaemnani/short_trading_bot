# Go-Live Runbook (P11)

Order: **backtest → paper(모의투자) → live(실전) smallest size → scale**. Never skip paper.

## 0. Prerequisites
- 한국투자증권 계좌 + KIS Developers API 신청 (apiportal.koreainvestment.com) → appkey/appsecret.
- 모의투자 계좌 신청 (paper). 해외주식 거래 시 통합증거금 약정 권장.
- ⚠️ Verify on the portal before live: order/balance **TR_IDs** (esp. overseas cancel), realtime
  field indices (H0STCNT0), per-second **rate limits**, current 증권거래세 rate.

## 1. Configure (`.env`, never commit)
```
STB_MODE=PAPER                # PAPER first
STB_DRY_RUN=true
STB_DB_URL=postgresql+asyncpg://...   # persistent (not :memory:)
STB_KIS__PAPER__APP_KEY=...   STB_KIS__PAPER__APP_SECRET=...   STB_KIS__PAPER__ACCOUNT_NO=...
STB_API_JWT_SECRET=<32+ random bytes>   STB_API_USERNAME=...   STB_API_PASSWORD=<strong>
```
Set risk limits **in code** when building the service (`RiskLimits(daily_loss_limit=…, max_open_positions=…,
max_order_notional=…, max_ticker_exposure=…)`).

## 2. Backtest + walk-forward
- `Backtester` / per-strategy params; validate with KR costs + walk-forward out-of-sample.
- Confirm CAGR/MDD/Sharpe/win-rate are acceptable and not overfit.

## 3. Paper (모의투자) — run for several weeks
- `trader preflight` → all critical checks OK.
- `trader serve` with `build_trading_service(settings, watchlist, limits=…)` + `KisWebSocketFeed`.
- `trader api` (behind HTTPS) + PWA dashboard for monitoring + remote pause/kill-switch.
- Verify: token/approval_key issuance, WS feed + BarBuilder, lot auto entry/manage/exit,
  **idempotent orders**, **reconcile after disconnect**, alerts, EOD settlement.
- Confirm paper fills ≈ backtest assumptions; fix divergences.

## 4. Live cutover (smallest size)
- Flip `STB_MODE=LIVE`, `STB_DRY_RUN=false`. Adapters switch base URL + TR_ID prefix automatically.
- Start at the **smallest size**; keep **kill switch + daily-loss limit armed at all times**.
- Watch the first sessions live; verify live == paper (orders, fills, reconcile, no EGW00201).
- Scale size up **gradually**, only after live matches paper.

## 5. Safety invariants (always on)
- Kill switch (긴급중지) → flat-all, reachable from any device.
- Daily loss limit, max positions, per-ticker exposure, per-order notional, ~15 req/s throttle.
- Reconcile against broker 잔고 before resuming after any gap.
- ⚠️ 환전 is NOT API-automatable (KIS) — overseas uses 통합증거금 auto-FX; explicit 환전 is manual.
- Commercial/legal: KIS API terms, 투자일임 규제, KR-FinBert/news license, 외국환거래법, 해외 양도소득세 22%.
