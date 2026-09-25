"""Command-line entrypoint (``trader``).

P0 wires config + logging + DB init and a ``serve`` placeholder. The asyncio engine
orchestrator (feed/indicator/strategy/order/reconciler) lands in later phases.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import typer

from ..infra.config import Settings, get_settings
from ..infra.logging import configure_logging, get_logger
from ..persistence.db import create_engine, init_models

app = typer.Typer(no_args_is_help=True, help="short_trading_bot — KIS 자동매매 봇 CLI")
campaign_app = typer.Typer(no_args_is_help=True, help="기간 한정 운용(Campaign) 관리")
app.add_typer(campaign_app, name="campaign")


def _bootstrap() -> Settings:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return settings


@app.command()
def version() -> None:
    """Print the package version."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    try:
        typer.echo(pkg_version("short-trading-bot"))
    except PackageNotFoundError:
        typer.echo("0.0.1 (dev)")


@app.command()
def config() -> None:
    """Show effective settings (secrets masked)."""
    s = _bootstrap()
    typer.echo(f"mode            = {s.mode.value}")
    typer.echo(f"dry_run         = {s.dry_run}")
    typer.echo(f"db_url          = {s.db_url}")
    typer.echo(f"log             = {s.log_level}/{s.log_format}")
    typer.echo(f"kis.paper       = {'configured' if s.kis.paper.configured else 'MISSING'}")
    typer.echo(f"kis.live        = {'configured' if s.kis.live.configured else 'MISSING'}")
    typer.echo(f"dart_api_key    = {'set' if s.dart_api_key else 'unset'}")
    typer.echo(f"active env       = {'live' if s.is_live else 'paper'}")


@app.command()
def initdb() -> None:
    """Create database tables (dev/tests; Alembic owns prod schema)."""
    s = _bootstrap()
    log = get_logger("initdb")

    async def _run() -> None:
        engine = create_engine(s.db_url)
        await init_models(engine)
        await engine.dispose()

    asyncio.run(_run())
    log.info("db.initialized", db_url=s.db_url)


@app.command()
def serve(
    config: str = "watchlist.json",
    live_exec: bool = False,
    fill_poll_seconds: float = 2.0,
) -> None:
    """Run the engine: load watchlist+limits, hydrate from DB, stream the KIS feed.

    Default execution is SIMULATED (PaperBroker) on live KIS data — a forward test.
    --live-exec uses the real KIS broker (routing 국내/해외) + a FillPoller (polls 체결내역,
    the authoritative fill record) + a startup reconcile against 잔고.
    """
    import asyncio

    from ..domain.enums import Mode, Resolution
    from ..execution.broker.paper import PaperBrokerAdapter
    from ..infra.kis_auth import KisAuth
    from ..market.kis_ws_feed import KisWebSocketFeed
    from .engine import build_broker, build_trading_service, kis_rest_base, kis_ws_url
    from .watchlist import load_paper_cash, load_scanner_config, load_trading_config

    s = _bootstrap()
    log = get_logger("serve")
    watchlist, limits = load_trading_config(config)
    scanner_cfg = load_scanner_config(config)
    paper_cash = load_paper_cash(config)
    if not watchlist and not scanner_cfg.enabled:
        typer.echo(f"watchlist empty — add entries to {config} (see watchlist.example.json).")
        raise typer.Exit(1)

    # 멀티 해상도: 한 WS 연결(틱)에서 필요한 모든 해상도의 봉을 동시에 집계한다.
    wanted = {tmpl.resolution for tmpl in watchlist.values()}
    if scanner_cfg.enabled:
        wanted.add(scanner_cfg.template().resolution)  # 스캐너 합류분의 봉 집계용
    resolutions = sorted(wanted, key=lambda r: r.value)
    tickers = sorted({key.split("@")[0] for key in watchlist})

    creds = s.active_kis()
    if not creds.configured:
        typer.echo("No KIS keys in .env — cannot stream live data. Fill STB_KIS__* then re-run.")
        raise typer.Exit(1)

    if live_exec and s.mode is Mode.LIVE:
        # 실전 실주문 게이트 (#13). 모의계좌 --live-exec 은 현행 유지.
        # STB_DRY_RUN 은 문서상 '실전 전환 전 리허설' 스위치 — 켜져 있으면 실주문 금지.
        if s.dry_run:
            typer.echo("STB_DRY_RUN=true — 실전(LIVE) 실주문을 거부합니다. 전환하려면 STB_DRY_RUN=false.")
            raise typer.Exit(1)
        from .engine import is_ready
        from .engine import preflight as run_preflight

        checks = run_preflight(s, limits=limits)
        if not is_ready(checks):
            for c in checks:
                if c.critical and not c.ok:
                    typer.echo(f"  preflight 실패: {c.name} — {c.detail}")
            typer.echo("실전 preflight 의 critical 항목이 통과하지 않아 가동을 거부합니다.")
            raise typer.Exit(1)

    from ..infra.notifier.factory import build_notifier
    from ..infra.rate_limit import configure_shared_limiter

    # KIS 초당 한도(계좌 단위·전 엔드포인트 합산): 모의 2건 / 실전 20건.
    # 한도에 딱 맞추면 서버측 계측 오차로 다시 초과하므로 보수적으로 잡는다.
    # 초과하면 500(EGW00201) + Connection: close → DNS 폭주 → 시세 연결 붕괴 (2026-08-11).
    configure_shared_limiter(*((1.5, 2.0) if s.mode is Mode.PAPER else (12.0, 15.0)))

    # 프로세스 전체가 KisAuth 하나를 공유한다 — KIS 는 토큰 발급 빈도를 제한해서,
    # 인스턴스를 여럿 만들면 재시작을 반복할 때 403 tokenP 로 막힌다 (2026-08-11).
    auth = KisAuth(creds, kis_rest_base(s.mode))

    if live_exec:
        broker = build_broker(s, auth=auth)
    else:
        from ..execution.broker.paper import PaperConfig

        broker = PaperBrokerAdapter(
            PaperConfig(initial_cash=paper_cash) if paper_cash else PaperConfig()
        )
    notifier = build_notifier(s)

    # 시장 레짐 필터: regime_filter가 켜진 템플릿/스캐너가 하나라도 있으면 활성.
    from ..market.regime import MarketRegime

    regime: MarketRegime | None = None
    if scanner_cfg.regime_filter or any(t.regime_filter for t in watchlist.values()):
        regime = MarketRegime()
        if regime.proxy_ticker not in tickers:
            tickers.append(regime.proxy_ticker)  # 프록시 시세는 항상 구독

    service = build_trading_service(
        s, watchlist, limits=limits, broker=broker, notifier=notifier, regime=regime
    )
    feed_ref: dict[str, KisWebSocketFeed | None] = {"feed": None}  # 스캐너 동적 구독용

    def _subscriptions() -> list[str]:
        """지금 시세가 필요한 종목 전부 — 매 (재)접속마다 새로 계산한다.

        tickers(설정 워치리스트 + 레짐 프록시) + service 가 추적 중인 종목(스캐너 합류·보유).
        정적 리스트만 쓰면 합류분이 재접속에서 유실되고, 합류분을 리스트에 쌓기만 하면
        만료분이 안 빠져 구독 한도를 채운다 — 파생이 양쪽을 동시에 푼다."""
        return sorted(set(tickers) | service.tracked_tickers())

    async def _poll_loop(poller: object) -> None:
        from datetime import datetime

        from ..execution.fill_poller import FillPoller
        from ..execution.poll_gate import KST, PollGate

        assert isinstance(poller, FillPoller)
        # UNKNOWN 주문 복구는 같은 주기로 동행 — UNKNOWN 이 없으면 API 호출 없이 즉시 반환.
        resolver = service.make_unknown_resolver()
        # 장외엔 폴링 자체를 쉼 (VTS 가 장외 체결내역 TR 을 500 으로 거부 — 2026-08-08).
        # 장중 연속 실패는 지수 백오프로 KIS 해머링 방지. 로직·근거는 poll_gate.py.
        gate = PollGate(base_seconds=fill_poll_seconds)
        was_idle = False
        while True:
            now = datetime.now(KST)
            if gate.in_session(now):
                if was_idle:
                    was_idle = False
                    log.info("fill_poll.session_start")
                    try:
                        # 새 거래일: 전일 미체결(장 마감으로 소멸) 주문을 만료 → 잠금 해제 (#6)
                        await service.expire_stale_orders()
                    except Exception:
                        log.exception("order.expire_stale.error")
                ok = True
                try:
                    await resolver.poll_once()
                except Exception:
                    ok = False
                    log.exception("unknown_resolve.error")
                try:
                    await poller.poll_once()
                except Exception:
                    ok = False
                    log.exception("fill_poll.error")
                gate.record(ok)
                service.health.on_poll(ok, now)
                if not ok and gate.consecutive_failures in (1, 5):
                    # 백오프 진입/지속을 한눈에 — 매 실패마다가 아니라 이정표만.
                    log.warning(
                        "fill_poll.backoff",
                        failures=gate.consecutive_failures,
                        delay_seconds=gate.next_delay(now),
                    )
                if not ok and gate.consecutive_failures == 5:
                    # 장중 5연속 실패 = 체결 감지가 실질 중단 — 사람에게 알린다 (1회성 이정표).
                    await notifier.notify(
                        "fill_poll.degraded", 연속실패=5, 조치="KIS 장애 여부 확인 필요"
                    )
            elif not was_idle:
                was_idle = True
                log.info("fill_poll.session_idle")
            await asyncio.sleep(gate.next_delay(datetime.now(KST)))

    def _backfill_daily(daily_tickers: list[str]) -> int:
        """일봉 워밍업 백필 (FinanceDataReader, 최근 ~200일)."""
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        import FinanceDataReader as fdr

        from ..market.types import Bar

        start = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%d")
        bars: list[Bar] = []
        for ticker in daily_tickers:
            try:
                df = fdr.DataReader(ticker, start)
            except Exception:
                log.warning("backfill.failed", ticker=ticker)
                continue
            for idx, row in df.iterrows():
                if row.isna().any() or row["Volume"] == 0:
                    continue
                c = Decimal(str(row["Close"]))
                v = Decimal(str(int(row["Volume"])))
                bars.append(
                    Bar(ticker, Resolution.D1,
                        datetime(idx.year, idx.month, idx.day, tzinfo=UTC),
                        Decimal(str(row["Open"])), Decimal(str(row["High"])),
                        Decimal(str(row["Low"])), c, v, c * v)
                )
        return service.prime(bars)

    async def _backfill_intraday(
        auth: KisAuth, resolution: Resolution, res_tickers: list[str]
    ) -> int:
        """분봉 워밍업 백필 (KIS 분봉, 최근 5거래일 — 디스크 캐시 우선)."""
        from datetime import date, timedelta

        from ..market.kis_history import KisMinuteHistory, resample

        hist = KisMinuteHistory(auth, creds, kis_rest_base(s.mode))
        days: list[date] = []
        d = date.today() - timedelta(days=1)
        while len(days) < 5:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        total = 0
        for ticker in res_tickers:
            try:
                one_min = await hist.fetch_days(ticker, days)
            except Exception:
                log.warning("backfill.intraday_failed", ticker=ticker)
                continue
            total += service.prime(
                one_min if resolution is Resolution.M1 else resample(one_min, resolution)
            )
        return total

    def _tickers_for(resolution: Resolution) -> list[str]:
        return sorted({
            key.split("@")[0] for key, tmpl in watchlist.items() if tmpl.resolution is resolution
        })

    # -- 장중 종목검색(스캐너): 급등+거래량 급증 종목 자동 합류 ---------------------

    def _refresh_favorites() -> set[str]:
        """며칠 일봉 스캔(거래량 상승 + 우상향) 후보군 — 완화 문턱으로 우선 합류.

        FDR 다운로드가 느려서(수 분) executor 스레드에서 하루 1회 돈다."""
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        import FinanceDataReader as fdr

        from ..market.scanner import scan_volume_leaders
        from ..market.types import Bar

        start = (datetime.now(UTC) - timedelta(days=120)).strftime("%Y-%m-%d")
        candidates: dict[str, tuple[str, list[Bar]]] = {}
        for market_name in ("KOSPI", "KOSDAQ"):
            try:
                listing = fdr.StockListing(market_name)
            except Exception:
                log.warning("scanner.listing_failed", market=market_name)
                continue
            if "Marcap" in listing.columns:
                listing = listing.sort_values("Marcap", ascending=False)
            for _, row in listing.head(scanner_cfg.daily_universe // 2).iterrows():
                code, name = str(row["Code"]), str(row["Name"])
                try:
                    df = fdr.DataReader(code, start)
                except Exception:
                    continue
                bars = []
                for idx, r in df.iterrows():
                    if r.isna().any() or r["Volume"] == 0:
                        continue
                    c = Decimal(str(r["Close"]))
                    v = Decimal(str(int(r["Volume"])))
                    bars.append(
                        Bar(code, Resolution.D1,
                            datetime(idx.year, idx.month, idx.day, tzinfo=UTC),
                            Decimal(str(r["Open"])), Decimal(str(r["High"])),
                            Decimal(str(r["Low"])), c, v, c * v)
                    )
                candidates[code] = (name, bars)
        leaders = scan_volume_leaders(candidates, top=scanner_cfg.daily_candidates)
        return {r.ticker for r in leaders}

    def _record_rankings(now: object, rows: list[Any]) -> None:
        """순위 스냅샷을 JSONL로 축적 — 실데이터 기반 사후 검증/재시뮬레이션의 원천."""
        import dataclasses
        import json as _json
        from pathlib import Path

        out = Path("data/rankings") / f"{now:%Y%m%d}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(
                {"ts": f"{now:%H:%M:%S}", "rows": [dataclasses.asdict(r) for r in rows]},
                ensure_ascii=False,
            ) + "\n")

    async def _scan_loop(auth: KisAuth) -> None:
        from datetime import date as _date
        from datetime import datetime, timedelta, timezone

        from ..market.kis_history import KisMinuteHistory, resample
        from ..market.kis_ranking import KisVolumeRank
        from ..market.scanner import MomentumPick, pick_momentum

        kst = timezone(timedelta(hours=9))
        scan_res = scanner_cfg.template().resolution
        # 순위분석 API는 모의 도메인 미지원 가능 → 라이브 키가 있으면 라이브 도메인으로
        # 시세만 조회한다 (주문과 무관, 안전).
        if s.kis.live.configured and s.mode is not Mode.LIVE:
            rank_creds = s.kis.live
            rank_base = kis_rest_base(Mode.LIVE)
            rank_auth = KisAuth(rank_creds, rank_base)
        else:
            rank_creds, rank_base, rank_auth = creds, kis_rest_base(s.mode), auth
        ranking = KisVolumeRank(rank_auth, rank_creds, rank_base)
        hist = KisMinuteHistory(auth, creds, kis_rest_base(s.mode))
        dart_checker = None
        if scanner_cfg.dart_filter and s.dart_api_key:
            from ..news.risk import DartRiskChecker

            dart_checker = DartRiskChecker(s.dart_api_key)
        favorites: set[str] = set()
        fav_day: _date | None = None
        joined_today: dict[_date, int] = {}

        async def _join(pick: MomentumPick, today: _date) -> bool:
            # 지표 워밍업: 오늘 1분봉을 캐시 없이 받아 스캔 해상도로 리샘플 후 프라임.
            try:
                one_min = await hist.fetch_day(pick.ticker, today, cache=False)
            except Exception:
                log.exception("scanner.backfill_failed", ticker=pick.ticker)
                return False
            service.prime(one_min if scan_res is Resolution.M1 else resample(one_min, scan_res))
            if not service.add_template(
                f"{pick.ticker}@scan", scanner_cfg.template(), one_shot=not scanner_cfg.rejoin
            ):
                return False
            # 현재 연결에는 즉시 구독. 재접속 후에는 _subscriptions() 가 service 상태에서
            # 다시 파생시키므로 별도 목록 관리가 필요 없다 (합류분 유실·누적 둘 다 방지).
            feed = feed_ref["feed"]
            if feed is not None:
                await feed.subscribe(pick.ticker)
            await notifier.notify(
                "scanner.joined", ticker=pick.ticker, name=pick.name,
                change_pct=f"{pick.change_pct:.1f}", vol_surge=f"{pick.vol_surge:.0f}",
                favorite=str(pick.favorite),
            )
            log.info("scanner.joined", ticker=pick.ticker, name=pick.name,
                     change_pct=pick.change_pct, favorite=pick.favorite)
            return True

        # 합류 마감 = 전략 entry_cutoff (진입 불가능한 늦은 합류가 WS 슬롯만 차지하는 것 방지).
        try:
            _h, _m = str(scanner_cfg.strategy_params.get("entry_cutoff", "14:00")).split(":")
            scan_end = min(int(_h) * 60 + int(_m), 14 * 60)
        except ValueError:
            scan_end = 14 * 60

        while True:
            try:
                now = datetime.now(kst)
                minute = now.hour * 60 + now.minute
                in_session = now.weekday() < 5 and (9 * 60 + 5) <= minute <= scan_end
                if in_session and not service.control.is_stopped:
                    today = now.date()
                    if scanner_cfg.daily_candidates and fav_day != today:
                        fav_day = today
                        try:
                            favorites = await asyncio.get_running_loop().run_in_executor(
                                None, _refresh_favorites
                            )
                            log.info("scanner.favorites", count=len(favorites))
                        except Exception:
                            log.exception("scanner.favorites_failed")
                    capacity = scanner_cfg.max_active - joined_today.get(today, 0)
                    if (
                        scanner_cfg.regime_filter
                        and regime is not None
                        and not regime.entries_allowed
                    ):
                        capacity = 0  # 시장 레짐 나쁨 → 이번 주기 합류 없음 (기존 랏 관리는 계속)
                    # KIS WS 등록 한도(~41) 보호: 여유 없으면 이번 주기는 건너뜀.
                    # 합류분까지 세는 실제 구독 수로 판정 (tickers 만 세면 과소계상 → 한도 초과).
                    if capacity > 0 and len(_subscriptions()) < 40:
                        rows = await ranking.top()
                        if scanner_cfg.record_rankings and rows:
                            _record_rankings(now, rows)  # 사후 검증/재시뮬레이션용 스냅샷
                        picks = pick_momentum(
                            rows,
                            min_change_pct=scanner_cfg.min_change_pct,
                            max_change_pct=scanner_cfg.max_change_pct,
                            min_vol_surge=scanner_cfg.min_vol_surge,
                            min_value=scanner_cfg.min_value_traded,
                            exclude=set(tickers),
                            favorites=favorites,
                            favorite_relax=scanner_cfg.favorite_relax,
                            top=capacity,
                        )
                        for pick in picks:
                            if dart_checker is not None and await dart_checker.is_risky(pick.ticker):
                                log.info("scanner.dart_blocked", ticker=pick.ticker)
                                continue
                            if await _join(pick, today):
                                joined_today[today] = joined_today.get(today, 0) + 1
            except Exception:
                log.exception("scanner.error")
            await asyncio.sleep(scanner_cfg.interval_seconds)

    def _regime_refresh() -> tuple[bool, Decimal | None]:
        """전일 코스피 종가 vs 20일선 + 프록시 ETF 전일 종가 (FDR, executor에서 실행)."""
        import FinanceDataReader as fdr

        kospi = fdr.DataReader("KS11").dropna().tail(40)
        closes = [float(c) for c in kospi["Close"]]
        ok = len(closes) >= 20 and closes[-1] > sum(closes[-20:]) / 20
        proxy = fdr.DataReader(regime.proxy_ticker).dropna().tail(3) if regime else None
        prev_close = (
            Decimal(str(float(proxy["Close"].iloc[-1]))) if proxy is not None and len(proxy) else None
        )
        return ok, prev_close

    async def _regime_loop() -> None:
        """하루 한 번(및 시작 시) 일 단위 레짐 갱신. 실패 시 이전 상태 유지."""
        from datetime import date as _date
        from datetime import datetime, timedelta, timezone

        kst = timezone(timedelta(hours=9))
        last: _date | None = None
        while True:
            today = datetime.now(kst).date()
            if today != last:
                try:
                    ok, prev_close = await asyncio.get_running_loop().run_in_executor(
                        None, _regime_refresh
                    )
                    regime.set_daily(ok, proxy_prev_close=prev_close)  # type: ignore[union-attr]
                    last = today
                    log.info("regime.refreshed", daily_ok=ok)
                except Exception:
                    log.exception("regime.refresh_failed")
            await asyncio.sleep(600)

    async def _eod_cache_loop(auth: KisAuth) -> None:
        """매 거래일 15:40 이후 당일 1분봉을 디스크 캐시에 저장 — 검증 데이터 자산화.

        분봉 API 조회 깊이(~1년) 한계 때문에 매일 쌓아둬야 장기 검증이 가능해진다.
        구독 중 종목(스캐너 합류분 포함) 전부. 이미 캐시된 날은 건너뜀."""
        from datetime import date as _date
        from datetime import datetime, timedelta, timezone

        from ..market.kis_history import KisMinuteHistory

        kst = timezone(timedelta(hours=9))
        hist = KisMinuteHistory(auth, creds, kis_rest_base(s.mode))
        done: _date | None = None
        while True:
            now = datetime.now(kst)
            if now.weekday() < 5 and now.hour * 60 + now.minute >= 15 * 60 + 40 and done != now.date():
                targets = sorted(set(tickers) | service.open_tickers())
                saved = 0
                for t in targets:
                    try:
                        bars = await hist.fetch_day(t, now.date())
                        saved += 1 if bars else 0
                    except Exception:
                        log.warning("eod_cache.failed", ticker=t)
                done = now.date()
                log.info("eod_cache.saved", tickers=saved, of=len(targets))
                try:
                    # 장 마감 일일 요약 — 매 거래일 1건. 카카오 토큰의 일일 keep-alive 도 겸한다
                    # (발송 시 만료 임박 토큰이 자동 갱신되므로 리프레시 토큰이 계속 연장됨).
                    snap = await service.status_snapshot()
                    lots = snap.get("open_lots")
                    await notifier.notify(
                        "daily.summary",
                        평가금=snap.get("equity"),
                        당일실현손익=snap.get("daily_realized"),
                        보유랏=len(lots) if isinstance(lots, list) else 0,
                    )
                except Exception:
                    log.exception("daily_summary.error")
            await asyncio.sleep(300)

    async def _feed_watchdog() -> None:
        """장중 시세 무소식 감시 — 엔진이 살아서 '관망만' 하는 조용한 고장을 잡는다.

        WS 가 끊기면 재접속 루프가 돌지만, 접속은 됐는데 데이터가 안 오는 경우
        (구독 실패·서버측 무응답)는 어디에도 안 잡힌다. 화면·알림 모두 정상으로 보인다."""
        from datetime import datetime

        from ..execution.poll_gate import KST

        alerted = False
        while True:
            await asyncio.sleep(60)
            now = datetime.now(KST)
            try:
                ok = service.health.feed_ok(now)
                if not ok and not alerted:
                    alerted = True
                    stale = service.health.feed_stale_seconds(now)
                    log.warning("feed.stale", stale_seconds=stale)
                    await notifier.notify(
                        "feed.stale",
                        무소식=(f"{stale / 60:.0f}분" if stale is not None else "봉 없음"),
                        조치="시세 연결 확인 필요",
                    )
                elif ok and alerted:
                    alerted = False  # 회복 시 다음 이상을 다시 알릴 수 있게
                    log.info("feed.recovered")
                    await notifier.notify("feed.recovered", 상태="시세 수신 정상화")
            except Exception:
                log.exception("feed_watchdog.error")

    async def _status_loop() -> None:
        """엔진 현황을 5초마다 data/engine_status.json에 기록 — 대시보드의 실시간 소스."""
        import json as _json
        from pathlib import Path

        out = Path("data/engine_status.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                snap = await service.status_snapshot()
                tmp = out.with_suffix(".tmp")
                tmp.write_text(_json.dumps(snap, ensure_ascii=False))
                tmp.replace(out)  # 원자적 교체
            except Exception:
                log.exception("status_write.error")
            await asyncio.sleep(5)

    async def _control_loop() -> None:
        """대시보드 제어 명령(data/control.json)을 2초마다 읽어 엔진에 적용.

        API는 별도 프로세스라 메모리 ControlSwitch가 공유되지 않는다 — 파일이 매개.
        seq 단조 증가로 같은 명령이 두 번 적용되지 않는다."""
        from ..risk.control_file import apply_command, read_command

        applied = 0
        cmd = read_command()
        if cmd is not None:
            applied = cmd[0]  # 시작 시점의 잔존 명령은 과거 것 — 새 명령부터 적용
        while True:
            try:
                cmd = read_command()
                if cmd is not None and cmd[0] > applied:
                    applied = cmd[0]
                    apply_command(service.control, cmd[1])
                    log.info("control_file.applied", action=cmd[1], seq=applied)
                    await notifier.notify("control", action=cmd[1], source="dashboard")
            except Exception:
                log.exception("control_read.error")
            await asyncio.sleep(2)

    async def _run() -> None:
        from ..risk.control_file import kill_switch_active

        restored = await service.hydrate()
        if kill_switch_active():
            # 긴급중지 도중 종료(크래시)된 뒤의 재기동 — 청산을 이어서 한다 (#7).
            service.control.stop()
            log.warning("kill_switch.restored_on_start")
            await notifier.notify("kill_switch.restored", 조치="재기동 — 전량청산 재개")
        # 스캐너로 합류했던(워치리스트 밖) 열린 랏의 시세는 _subscriptions() 가 파생시킨다 —
        # 여기서 tickers 에 영구 추가하면 랏 청산 후에도 구독이 남아 한도를 잠식한다.
        if live_exec:
            report = await service.reconcile()
            if not report.in_sync:
                log.warning("reconcile.drift_on_start", mismatches=len(report.mismatches))
                await notifier.notify(
                    "reconcile.drift", 불일치=len(report.mismatches), 시점="엔진 시작"
                )
        # auth 는 위에서 만든 공용 인스턴스를 그대로 쓴다 (토큰 발급 빈도 제한 때문).
        # 지표 워밍업 백필 — 해상도별로. 없으면 저빈도 전략은 수십 거래일간 관망만 한다.
        primed = 0
        for res in resolutions:
            res_tickers = _tickers_for(res)
            if res is Resolution.D1:
                primed += _backfill_daily(res_tickers)
            elif res.is_intraday and res is not Resolution.TICK:
                primed += await _backfill_intraday(auth, res, res_tickers)
        log.info(
            "backfill.primed", bars=primed,
            resolutions=[r.value for r in resolutions],
        )
        log.info(
            "engine.start",
            mode=s.mode.value,
            tickers=len(_subscriptions()),
            entries=len(watchlist),
            resolutions=[r.value for r in resolutions],
            restored=restored,
            execution="live" if live_exec else "simulated",
        )
        # 시작 알림 — 워치독이 죽은 엔진을 되살렸을 때도 이 알림으로 "죽었었다"를 알게 된다.
        await notifier.notify(
            "engine.start",
            mode=s.mode.value,
            체결=("실주문" if live_exec else "시뮬"),
            복원랏=restored,
        )
        poll_task = (
            asyncio.create_task(_poll_loop(service.make_fill_poller())) if live_exec else None
        )
        scan_task = asyncio.create_task(_scan_loop(auth)) if scanner_cfg.enabled else None
        regime_task = asyncio.create_task(_regime_loop()) if regime is not None else None
        eod_task = asyncio.create_task(_eod_cache_loop(auth))
        status_task = asyncio.create_task(_status_loop())
        control_task = asyncio.create_task(_control_loop())
        feed_watch_task = asyncio.create_task(_feed_watchdog())
        from ..market.bar_builder import BarBuilder

        # 해상도별 공유 빌더: 한 WS 연결의 틱을 모든 해상도로 동시 집계, 재접속에도 봉 보존.
        shared_builders = [BarBuilder(res) for res in resolutions]
        def _done() -> bool:
            # 긴급중지 후에도 청산이 끝날 때까지는 시세·체결 폴링을 유지해야 한다 — 예전엔
            # STOP 후 WS 가 한 번 끊기면 청산 체결 확인 전에 종료했다 (#7).
            return service.control.is_stopped and service.is_flat()

        try:
            # Reconnect loop: a WS disconnect ends the stream; resume until 긴급중지+청산 완료.
            while not _done():
                try:
                    approval = await auth.approval_key()
                except Exception:
                    # 승인키 발급 일시 실패로 프로세스가 죽으면 청산·손절 관리가 통째로 멈춘다.
                    log.exception("ws.approval_key_failed")
                    await asyncio.sleep(5)
                    continue
                subs = _subscriptions()  # 재접속마다 최신 목록 (합류분 포함·만료분 제외)
                feed = KisWebSocketFeed(
                    approval, subs, resolutions[0], ws_url=kis_ws_url(s.mode),
                    bar_builders=shared_builders, flush_on_close=False,
                )
                feed_ref["feed"] = feed  # 스캐너가 현재 연결에 동적 구독할 수 있도록
                try:
                    await service.run(feed)
                except Exception:
                    log.exception("feed.error")
                if _done():
                    break
                if live_exec:  # recover anything missed while disconnected
                    try:
                        await service.make_fill_poller().poll_once()
                        await service.reconcile()
                    except Exception:
                        # 복구 조회의 일시 실패(REST 5xx 등)로 엔진이 죽으면 안 된다 —
                        # 2026-08-03 해외 잔고 500이 여기서 미보호로 전파돼 나흘 다운.
                        log.exception("reconnect_recover.error")
                service.health.on_feed_connect()
                log.info("feed.reconnect", delay_seconds=5)
                await asyncio.sleep(5)
        finally:
            if poll_task is not None:
                poll_task.cancel()
            if scan_task is not None:
                scan_task.cancel()
            if regime_task is not None:
                regime_task.cancel()
            eod_task.cancel()
            status_task.cancel()
            control_task.cancel()
            feed_watch_task.cancel()
            from ..infra.http import close_shared_client

            await close_shared_client()

    asyncio.run(_run())
    if service.control.is_stopped and service.is_flat():
        # 긴급중지로 인한 종료 — 워치독(run_paper.sh --watchdog)이 되살리지 않게
        # 마커를 남긴다. 재개는 사용자가 ./run_paper.sh (마커 제거) 로만.
        from pathlib import Path

        Path("data").mkdir(exist_ok=True)
        Path("data/engine_stopped.marker").write_text("kill-switch")
        log.info("engine.stop_marker_written")


@app.command("api")
def serve_api(host: str | None = None, port: int = 8000) -> None:
    """Serve the dashboard API + WebSocket (FastAPI). Put HTTPS in front for production.

    기본 바인딩은 STB_API_HOST(기본 0.0.0.0 — 같은 와이파이의 폰에서 접속). 기본 자격증명이
    남아 있으면 loopback(--host 127.0.0.1)으로만 기동된다."""
    import os

    import uvicorn

    from ..api.security import insecure_api_config, is_loopback_host

    s = _bootstrap()
    bind = host or s.api_host
    problems = insecure_api_config(s.api_jwt_secret, s.api_password)
    if problems and not is_loopback_host(bind):
        typer.echo(
            f"대시보드 API 기동 거부 ({bind}): " + "; ".join(problems) + "\n"
            "  .env 에 STB_API_JWT_SECRET=$(openssl rand -hex 32) 와 STB_API_PASSWORD 를 설정하거나,\n"
            "  이 컴퓨터에서만 쓸 거면 `trader api --host 127.0.0.1`."
        )
        raise typer.Exit(1)
    # 앱 팩토리(api/main.py)도 같은 검사를 하므로 실제 바인딩 주소를 넘겨준다.
    os.environ["STB_API_HOST"] = bind
    get_settings.cache_clear()
    get_logger("api").info("api.start", host=bind, port=port, mode=s.mode.value)
    uvicorn.run(
        "short_trading_bot.api.main:create_default_app", factory=True, host=bind, port=port
    )


@app.command()
def scan(
    market: str = "KOSPI",
    limit: int = 100,
    top: int = 15,
    days: int = 120,
    min_vol_ratio: float = 1.2,
) -> None:
    """거래량 상승 + 우상향 종목 스캔 (watchlist 후보). 시총 상위 limit개 대상."""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    try:
        import FinanceDataReader as fdr
    except ImportError:
        typer.echo("FinanceDataReader가 필요합니다: .venv/bin/pip install finance-datareader")
        raise typer.Exit(1) from None

    from ..domain.enums import Resolution
    from ..market.scanner import scan_volume_leaders
    from ..market.types import Bar

    _bootstrap()
    listing = fdr.StockListing(market)
    if "Marcap" in listing.columns:
        listing = listing.sort_values("Marcap", ascending=False)
    listing = listing.head(limit)
    start = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")

    candidates: dict[str, tuple[str, list[Bar]]] = {}
    for _, row in listing.iterrows():
        code, name = str(row["Code"]), str(row["Name"])
        try:
            df = fdr.DataReader(code, start)
        except Exception:
            continue
        bars = []
        for idx, r in df.iterrows():
            if r.isna().any() or r["Volume"] == 0:
                continue
            c = Decimal(str(r["Close"]))
            v = Decimal(str(int(r["Volume"])))
            bars.append(
                Bar(
                    ticker=code, resolution=Resolution.D1,
                    ts=datetime(idx.year, idx.month, idx.day, tzinfo=UTC),
                    open=Decimal(str(r["Open"])), high=Decimal(str(r["High"])),
                    low=Decimal(str(r["Low"])), close=c, volume=v, value=c * v,
                )
            )
        candidates[code] = (name, bars)

    leaders = scan_volume_leaders(candidates, min_vol_ratio=min_vol_ratio, top=top)
    if not leaders:
        typer.echo("조건에 맞는 종목 없음 (거래량 증가 + 우상향)")
        return
    typer.echo(f"{'코드':<8}{'종목':<12}{'거래량비':>8}{'당일RVOL':>10}{'거래대금(억)':>12}{'종가':>10}")
    for r in leaders:
        typer.echo(
            f"{r.ticker:<8}{r.name:<12}{r.vol_ratio:>7.2f}x{r.rvol_today:>9.2f}x"
            f"{r.avg_value / 1e8:>11.0f}{r.close:>10,.0f}"
        )


@app.command("select-universe")
def select_universe(
    top: int = 5,
    universe: int = 200,
    write_json: bool = False,
) -> None:
    """눌림목 모델 적합 종목 자동 발굴 — 장기 상승추세 + 저변동 + 유동성.

    25종목 검증 기준(종가>MA120·정배열·ATR%≤5%·거래대금≥50억)으로 시총 상위
    ``universe``개를 걸러 6개월 수익률 상위 ``top``개를 추천한다.
    ``--write-json``이면 watchlist entries JSON을 출력 (가드 켠 상태).
    """
    import json as _json
    import warnings
    from datetime import UTC, datetime, timedelta

    warnings.filterwarnings("ignore")
    try:
        import FinanceDataReader as fdr
        import yfinance as yf
    except ImportError:
        typer.echo("FinanceDataReader + yfinance가 필요합니다 (.venv/bin/pip install ...)")
        raise typer.Exit(1) from None

    from ..domain.enums import Resolution
    from ..market.scanner import select_pullback_universe
    from ..market.types import Bar

    s = _bootstrap()
    listing = fdr.StockListing("KRX")
    suffix = {"KOSPI": ".KS"}
    rows = []
    for _, r in listing.iterrows():
        m = str(r.get("Market", ""))
        if m == "KOSPI" or m.startswith("KOSDAQ"):
            rows.append((str(r["Code"]), str(r["Name"]), suffix.get(m, ".KQ")))
    if "Marcap" in listing.columns:
        pass  # StockListing('KRX')는 이미 시총 정렬
    rows = rows[:universe]

    start = (datetime.now(UTC) - timedelta(days=260)).strftime("%Y-%m-%d")
    candidates: dict[str, tuple[str, list[Bar]]] = {}
    batch = 300
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        df = yf.download([c + s for c, _, s in chunk], start=start, interval="1d",
                         group_by="ticker", progress=False, threads=True)
        for code, name, suf in chunk:
            try:
                sub = df[code + suf].dropna(subset=["Close"])
            except Exception:
                continue
            sub = sub[sub["Volume"] > 0]
            bars = []
            for idx, r in sub.iterrows():
                c = Decimal(str(round(float(r["Close"]), 2)))
                v = Decimal(str(int(r["Volume"])))
                bars.append(Bar(ticker=code, resolution=Resolution.D1,
                                ts=datetime(idx.year, idx.month, idx.day, tzinfo=UTC),
                                open=Decimal(str(round(float(r["Open"]), 2))),
                                high=Decimal(str(round(float(r["High"]), 2))),
                                low=Decimal(str(round(float(r["Low"]), 2))),
                                close=c, volume=v, value=c * v))
            candidates[code] = (name, bars)

    picks = select_pullback_universe(candidates, top=top)
    if not picks:
        typer.echo("적합 종목 없음 (장기 상승추세 + 저변동 + 유동성 기준)")
        return

    # DART 위험공시 경고 (최근 90일 관리종목·감사의견 등) — 장기 거래정지 꼬리 리스크 경보.
    warns: dict[str, list[str]] = {}
    if s.dart_api_key:
        import asyncio as _asyncio

        from ..news.risk import DartRiskChecker

        async def _check() -> None:
            checker = DartRiskChecker(s.dart_api_key, lookback_days=90)
            for p in picks:
                warns[p.ticker] = await checker.risk_filings(p.ticker)

        _asyncio.run(_check())

    typer.echo(f"{'코드':<8}{'종목':<14}{'6개월수익':>10}{'ATR%':>7}{'거래대금(억)':>12}  공시경고")
    for p in picks:
        w = warns.get(p.ticker, [])
        flag = f"⚠️ {w[0][:20]}" if w else "-"
        typer.echo(f"{p.ticker:<8}{p.name:<14}{p.ret_6m:>+9.1%}{p.atr_pct:>6.1%}"
                   f"{p.avg_value / 1e8:>11.0f}  {flag}")
    if write_json:
        entries = {
            f"{p.ticker}@1D": {
                "strategy_id": "pullback_daily_v1", "market": "KRX", "resolution": "1D",
                "risk_per_trade": 0.02,
                "strategy_params": {
                    "rsi_min": 35, "rsi_max": 65, "touch_band_pct": 0.02,
                    "bull_risk_mult": 2.0, "max_adds": 1,
                    "require_above_sma120": True, "max_atr_pct": 0.05,
                },
            }
            for p in picks
        }
        typer.echo(_json.dumps(entries, ensure_ascii=False, indent=2))


@app.command("factor-picks")
def factor_picks(
    top: int = 10,
    universe: int = 200,
    budget: float = 0.0,
) -> None:
    """팩터(저PBR+흑자) 분기 리밸런스 종목 추출 — 10년 검증 채택 전략의 실행 도구.

    시총 상위 ``universe``개의 직전 사업연도 재무(DART)를 받아 저PBR + 흑자
    상위 ``top``개를 동일가중으로 추천한다 (검증: 10년 +353%, 2022 하락장 0%,
    눌림목과 월상관 -0.01). ``--budget``(원)을 주면 종목별 편입 수량까지 계산.

    운용 규칙: 분기마다 재실행해 목록대로 교체(리밸런스). 슬리브 배분은 자본의
    20~30% 권장 (MDD 46% 관리). 재무는 Y+1년 7월부터 Y년 것을 쓴다 (선견 차단).
    """
    import asyncio as _asyncio
    from datetime import date as _date

    try:
        import FinanceDataReader as fdr
    except ImportError:
        typer.echo("FinanceDataReader가 필요합니다: .venv/bin/pip install finance-datareader")
        raise typer.Exit(1) from None

    from ..market.fundamentals import (
        DartFundamentals,
        FundamentalRow,
        applicable_fiscal_year,
        select_factor_picks,
    )

    s = _bootstrap()
    if not s.dart_api_key:
        typer.echo("DART 키가 필요합니다 — .env.local에 STB_DART_API_KEY를 설정하세요.")
        raise typer.Exit(1)

    fiscal_year = applicable_fiscal_year(_date.today())
    listing = fdr.StockListing("KRX")  # 이미 시총 내림차순
    targets: list[tuple[str, str, Decimal, Decimal]] = []  # code, name, marcap, close
    for _, r in listing.iterrows():
        m = str(r.get("Market", ""))
        if not (m == "KOSPI" or m.startswith("KOSDAQ")):
            continue
        try:
            marcap = Decimal(str(int(r["Marcap"])))
            close = Decimal(str(float(r["Close"])))
        except (TypeError, ValueError):
            continue
        if marcap <= 0 or close <= 0:
            continue
        targets.append((str(r["Code"]), str(r["Name"]), marcap, close))
        if len(targets) >= universe:
            break

    typer.echo(f"{fiscal_year}년 사업보고서 기준, 시총 상위 {len(targets)}개 재무 조회 중 …")

    async def _collect() -> list[FundamentalRow]:
        dart = DartFundamentals(s.dart_api_key)
        rows: list[FundamentalRow] = []
        for i, (code, name, marcap, close) in enumerate(targets, start=1):
            fin = await dart.fetch(code, fiscal_year)
            if fin is not None:
                rows.append(FundamentalRow(
                    ticker=code, name=name, marcap=marcap, close=close,
                    equity=fin[0], net_income=fin[1], fiscal_year=fiscal_year,
                ))
            if i % 50 == 0:
                typer.echo(f"  … {i}/{len(targets)} (재무 확보 {len(rows)})")
        return rows

    rows = _asyncio.run(_collect())
    picks = select_factor_picks(rows, top=top)
    if not picks:
        typer.echo("조건(저PBR + 흑자)에 맞는 종목 없음 — DART 응답을 확인하세요.")
        raise typer.Exit(1)

    # 저PBR엔 부실주 위험이 따르므로 최근 90일 위험공시를 경고로 같이 보여준다.
    warns: dict[str, list[str]] = {}

    async def _check_warns() -> None:
        from ..news.risk import DartRiskChecker

        checker = DartRiskChecker(s.dart_api_key, lookback_days=90)
        for p in picks:
            warns[p.ticker] = await checker.risk_filings(p.ticker)

    _asyncio.run(_check_warns())

    per_stock = Decimal(str(budget)) * Decimal(str(picks[0].weight)) if budget > 0 else None
    header = f"{'코드':<8}{'종목':<14}{'PBR':>6}{'순이익(억)':>11}{'시총(조)':>9}{'종가':>10}"
    if per_stock is not None:
        header += f"{'편입수량':>9}"
    typer.echo(header + "  공시경고")
    for p in picks:
        w = warns.get(p.ticker, [])
        flag = f"⚠️ {w[0][:20]}" if w else "-"
        line = (
            f"{p.ticker:<8}{p.name:<14}{p.pbr:>6.2f}{float(p.net_income) / 1e8:>11,.0f}"
            f"{float(p.marcap) / 1e12:>9.2f}{float(p.close):>10,.0f}"
        )
        if per_stock is not None:
            line += f"{int(per_stock / p.close):>9,}"
        typer.echo(line + f"  {flag}")
    typer.echo(
        f"\n동일가중 {len(picks)}종목 (각 {picks[0].weight:.1%})"
        + (f", 종목당 예산 {float(per_stock):,.0f}원" if per_stock is not None else "")
        + " — 분기마다 재실행해 목록대로 리밸런스하세요."
    )


@app.command()
def preflight() -> None:
    """Go-live readiness checks (run before flipping STB_MODE=LIVE)."""
    from .engine import is_ready
    from .engine import preflight as run_preflight

    s = _bootstrap()
    checks = run_preflight(s)
    for c in checks:
        mark = "OK" if c.ok else ("!!" if c.critical else "--")
        typer.echo(f"[{mark}] {c.name}: {c.detail}")
    ready = is_ready(checks)
    typer.echo(f"\nready={ready} (mode={s.mode.value})")
    if not ready:
        raise typer.Exit(code=1)


@app.command("selfcheck")
def selfcheck(ticker: str = "122630", skip_orders: bool = False) -> None:
    """라이브 기본기능 점검 — 인증·시세·잔고·체결내역·매수·매도·취소가 실제로 되는지.

    주문은 **체결 불가 지정가 1주**로 넣고 즉시 취소한다 (비파괴). 잔고·포지션 불변.
    장 시작 전/후 아무 때나 실행 가능하며, 거부 사유가 '잔고/수량 부족'이면 통과로 본다
    (API 계약은 정상이라는 뜻). '전문 형식 오류'만 실패로 잡는다.

    2026-08-11: 매도가 8일간 100% 거부되던 것을 손절 신호가 뜬 뒤에야 발견 —
    그 재발을 막기 위한 상시 점검 도구.
    """
    import asyncio
    from decimal import Decimal

    from ..domain.enums import Mode, Side
    from ..infra.kis_auth import KisAuth
    from ..infra.rate_limit import configure_shared_limiter
    from .engine import build_broker, kis_rest_base
    from .selfcheck import CheckResult, _order_roundtrip

    s = _bootstrap()
    creds = s.active_kis()
    if not creds.configured:
        typer.echo("KIS 키가 없습니다 (.env.local 의 STB_KIS__*).")
        raise typer.Exit(1)
    # 점검은 느려도 되고, 대개 **봇이 돌고 있는 중**에 실행된다. KIS 한도는 계좌 단위라
    # 두 프로세스의 호출이 합산되므로, 점검은 봇 몫을 침범하지 않게 절반 속도로 돈다.
    configure_shared_limiter(*((0.7, 1.0) if s.mode is Mode.PAPER else (5.0, 5.0)))

    async def _run() -> list[CheckResult]:
        from ..infra.http import close_shared_client
        from ..market.kis_history import KisMinuteHistory
        from ..market.kis_ranking import KisVolumeRank

        out: list[CheckResult] = []
        base = kis_rest_base(s.mode)
        auth = KisAuth(creds, base)
        broker = build_broker(s, auth=auth)  # 토큰 공유 (발급 빈도 제한 회피)

        async def check(name: str, coro: Any) -> Any:
            try:
                return await coro
            except Exception as exc:
                text = str(exc)[:160]
                if "403" in text and "tokenP" in text:
                    # KIS 는 토큰 발급 자체에 빈도 제한이 있다 — 연달아 점검하면 걸린다.
                    text = "403 토큰 발급 한도 — 1분 후 재시도 (키 문제 아님)"
                out.append(CheckResult(name, False, text))
                return None

        # 1) 인증
        token = await check("인증 (access token)", auth.access_token())
        if token:
            out.append(CheckResult("인증 (access token)", True, f"발급됨 ({len(token)}자)"))
        approval = await check("인증 (WS approval key)", auth.approval_key())
        if approval:
            out.append(CheckResult("인증 (WS approval key)", True, "발급됨"))

        # 2) 잔고 조회
        balance = await check("잔고 조회", broker.get_balance())
        if balance is not None:
            cash = sum(balance.cash.values())
            out.append(
                CheckResult("잔고 조회", True, f"현금 {cash:,.0f} / 보유 {len(balance.positions)}종목")
            )

        # 3) 체결내역 조회 (FillPoller 의 근거)
        execs = await check("체결내역 조회", broker.get_executions())
        if execs is not None:
            out.append(CheckResult("체결내역 조회", True, f"{len(execs)}건"))

        # 4) 주문내역 조회 (UNKNOWN 복구의 근거)
        orders = await check("주문내역 조회", broker.get_daily_orders())
        if orders is not None:
            out.append(CheckResult("주문내역 조회", True, f"{len(orders)}건"))

        # 5) 분봉 조회 (지표 워밍업의 근거)
        from datetime import datetime

        from ..execution.poll_gate import KST

        hist = KisMinuteHistory(auth, creds, base)
        bars = await check(
            "분봉 조회", hist.fetch_day(ticker, datetime.now(KST).date(), cache=False)
        )
        if bars is not None:
            out.append(CheckResult("분봉 조회", bool(bars), f"{len(bars)}봉"))

        # 6) 순위 조회 (스캐너의 근거) — 모의 도메인은 순위 TR 을 지원하지 않아 500 이다.
        #    봇(serve)과 동일하게, 실전 키가 있으면 실전 도메인으로 조회한다 (시세만, 안전).
        if s.kis.live.configured and s.mode is not Mode.LIVE:
            rk_creds, rk_base = s.kis.live, kis_rest_base(Mode.LIVE)
            rk_auth = KisAuth(rk_creds, rk_base)
        else:
            rk_creds, rk_base, rk_auth = creds, base, auth
        rows = await check("순위 조회 (스캐너)", KisVolumeRank(rk_auth, rk_creds, rk_base).top())
        if rows is not None:
            out.append(CheckResult("순위 조회 (스캐너)", bool(rows), f"{len(rows)}종목"))

        # 7) 현재가 — 주문 가격 산정의 기준
        last: Decimal | None = None
        if bars:
            last = bars[-1].close
            out.append(CheckResult("현재가 확보", True, f"{ticker} {last:,.0f}"))
        else:
            out.append(CheckResult("현재가 확보", False, "분봉이 없어 주문 테스트 불가"))

        # 8) 매수/매도 주문 + 취소 (체결 불가 지정가 1주 → 즉시 취소)
        if skip_orders:
            out.append(CheckResult("주문 경로", True, "건너뜀 (--skip-orders)"))
        elif last is not None:
            from ..execution.tick_size import round_to_tick

            for side, mult, label in (
                (Side.BUY, Decimal("0.90"), "매수 주문 + 취소"),
                (Side.SELL, Decimal("1.10"), "매도 주문 + 취소"),
            ):
                try:
                    # 호가단위로 정렬하지 않으면 "호가단위 오류" 로 거부돼 정작 검증하려는
                    # 전문 형식 문제를 못 본다.
                    ok, detail = await _order_roundtrip(
                        broker, ticker, side, round_to_tick(last * mult, side)
                    )
                except Exception as exc:  # 점검 도구가 트레이스백으로 죽으면 안 된다
                    ok, detail = False, str(exc)[:160]
                out.append(CheckResult(label, ok, detail))
        else:
            out.append(CheckResult("주문 경로", False, "현재가를 못 구해 검증 불가"))

        # 9) 환전 — KIS 는 on-demand 환전 REST API 가 없다 (통합증거금 자동환전)
        out.append(
            CheckResult(
                "환전",
                True,
                "N/A — KIS 에 on-demand 환전 API 없음 (통합증거금 자동환전). 해외거래 "
                + ("사용" if s.overseas_enabled else "미사용이라 무관"),
            )
        )
        await close_shared_client()
        return out

    results = asyncio.run(_run())
    typer.echo(f"\n라이브 기본기능 점검 (mode={s.mode.value}, 종목={ticker})")
    typer.echo("-" * 72)
    for r in results:
        typer.echo(f"[{r.mark}] {r.name:<22} {r.detail}")
    failed = [r for r in results if not r.ok]
    typer.echo("-" * 72)
    typer.echo(f"{len(results) - len(failed)}/{len(results)} 통과")
    if failed:
        typer.echo("실패: " + ", ".join(r.name for r in failed))
        raise typer.Exit(1)


@app.command("kakao-auth")
def kakao_auth(port: int = 8899, wait_minutes: float = 15.0, code: str = "") -> None:
    """카카오톡 나에게 보내기 최초 1회 승인 — 브라우저 로그인 → 토큰 저장 → 테스트 발송.

    사전 준비 (developers.kakao.com, 5분):
    1) 내 애플리케이션 > 앱 선택/생성 → [앱] > [플랫폼 키] 에서 REST API 키 복사
       → .env.local 에 STB_NOTIFIER__KAKAO_REST_API_KEY=<키>
    2) 같은 화면의 REST API 키 상세 > [리다이렉트 URI] 에 http://localhost:8899/kakao 등록
       ※ 2026 콘솔 개편으로 위치가 '제품 설정 > 카카오 로그인' 에서 여기로 이동했다.
         미등록 시 승인 페이지가 KOE006 (앱 관리자 설정 오류) 로 막힌다.
       ※ 키를 복사한 그 앱에 등록해야 한다 — 다른 앱에 등록하면 계속 KOE006.
    3) 같은 REST API 키 상세의 [클라이언트 시크릿] 값을
       → .env.local 에 STB_NOTIFIER__KAKAO_CLIENT_SECRET=<값>
       ※ 신규 REST API 키는 이 기능이 기본 활성화 상태로 발급된다. 빠뜨리면 토큰 교환이
         KOE010 (Bad client credentials) 로 거부된다. 개편 콘솔에 별도 [보안] 메뉴는 없다.
    4) 제품 설정 > 카카오 로그인 활성화 ON
    5) 카카오 로그인 > 동의항목에서 '카카오톡 메시지 전송(talk_message)' 활성화

    ``--code``: 브라우저 자동 수신이 안 될 때(원격 셸·방화벽·다른 기기에서 승인 등)
    리다이렉트된 주소창의 ``?code=...`` 값을 직접 붙여넣는 우회로. 인가 코드는 발급 후
    10분·1회용이므로 승인 직후 바로 실행할 것.
    """
    import asyncio
    import time
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path
    from urllib.parse import parse_qs, quote, urlparse

    from ..infra.notifier.kakao import KakaoNotifier, exchange_auth_code, extract_auth_code

    s = _bootstrap()
    key = s.notifier.kakao_rest_api_key
    if not key:
        typer.echo("STB_NOTIFIER__KAKAO_REST_API_KEY 가 .env.local 에 없습니다 (docstring 참조).")
        raise typer.Exit(1)

    redirect_uri = f"http://localhost:{port}/kakao"

    secret = s.notifier.kakao_client_secret

    def _save_and_test(auth_code: str) -> None:
        async def _finish() -> None:
            token = await exchange_auth_code(
                key, redirect_uri, auth_code, client_secret=secret
            )
            token.save(Path(s.notifier.kakao_token_path))
            if not token.has_talk_message:
                # 콘솔 동의항목이 꺼져 있으면 카카오가 scope 없는 토큰을 조용히 준다 —
                # 발송 403(-402) 대신 여기서 원인을 짚어준다.
                raise RuntimeError(
                    "발급된 토큰에 talk_message 권한이 없습니다 "
                    f"(부여된 동의항목: {token.scope or '없음'}). "
                    "제품 설정 > 카카오 로그인 > 동의항목에서 '카카오톡 메시지 전송' 을 "
                    "'이용 중 동의' 로 켠 뒤 승인을 다시 받아야 합니다."
                )
            notifier = KakaoNotifier(
                key, s.notifier.kakao_token_path, client_secret=secret
            )
            await notifier.notify("kakao.connected", 설명="이제 봇 알림이 이 채널로 옵니다")

        try:
            asyncio.run(_finish())
        except Exception as exc:  # 코드 만료/재사용/불일치는 흔한 실사용 실패 — 안내로 받는다
            typer.echo(f"교환 실패: {exc}")
            if "KOE010" in str(exc):
                typer.echo(
                    "→ 클라이언트 시크릿 누락/불일치입니다. [앱] > [플랫폼 키] > [REST API 키] 상세의 "
                    "[클라이언트 시크릿] 값을 .env.local 의 "
                    "STB_NOTIFIER__KAKAO_CLIENT_SECRET 에 넣으세요 "
                    "(신규 REST API 키는 이 기능이 기본 활성화라 대개 필수)."
                )
            else:
                typer.echo(
                    "인가 코드는 1회용·10분 유효입니다 — 승인 페이지를 다시 열어 새 코드로 재시도하세요."
                )
            raise typer.Exit(1) from exc
        typer.echo(f"토큰 저장 완료: {s.notifier.kakao_token_path}")
        typer.echo("카카오톡 '나와의 채팅'에 테스트 메시지가 도착했는지 확인하세요.")

    if code:  # 수동 우회로 — 브라우저 대기 없이 코드만 교환
        _save_and_test(extract_auth_code(code))
        return

    auth_url = (
        "https://kauth.kakao.com/oauth/authorize"
        f"?client_id={key}&redirect_uri={quote(redirect_uri, safe='')}"
        "&response_type=code&scope=talk_message"
    )
    typer.echo("브라우저에서 카카오 로그인 후 [동의하고 계속하기]. 창이 안 열리면 직접 접속:")
    typer.echo(f"  {auth_url}")
    typer.echo(f"(대기 {wait_minutes:g}분. 자동 수신이 안 되면 리다이렉트 주소창의 code= 값으로")
    typer.echo(" `trader kakao-auth --code <값>` 을 실행하세요.)")
    webbrowser.open(auth_url)

    captured: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            got_code, got_error = qs.get("code", [""])[0], qs.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if got_code or got_error:
                captured["code"], captured["error"] = got_code, got_error
                body = "<h3>승인 완료 — 터미널로 돌아가세요.</h3>"
            else:
                body = "<h3>대기 중…</h3>"  # favicon 등 부수 요청은 소비만 하고 계속 대기
            self.wfile.write(body.encode())

        def log_message(self, format: str, *args: object) -> None:
            pass  # 기본 stderr 액세스 로그 침묵

    deadline = time.monotonic() + wait_minutes * 60
    try:
        server_ctx = HTTPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:  # 다른 프로세스가 그 포트를 쓰는 중 (직접 띄운 http.server 등)
        typer.echo(f"포트 {port} 를 열 수 없습니다: {exc}")
        typer.echo(f"  이미 무언가 {port} 를 쓰고 있습니다 — 그 프로세스를 끄거나,")
        typer.echo("  승인 후 주소창의 code= 값으로 `trader kakao-auth --code <값>` 을 쓰세요")
        typer.echo("  (리다이렉트 URI 가 그 포트로 등록돼 있으므로 포트 변경은 재등록이 필요).")
        raise typer.Exit(1) from exc
    with server_ctx as server:
        server.timeout = 5.0  # 짧게 끊어 받으며 deadline 을 직접 관리
        # 콜백 외 부수 요청(favicon 등)이 대기를 소비하지 않도록 코드 수신까지 반복.
        while not captured and time.monotonic() < deadline:
            server.handle_request()

    if captured.get("error") or not captured.get("code"):
        reason = captured.get("error") or f"코드 미수신 ({wait_minutes:g}분 타임아웃)"
        typer.echo(f"승인 실패: {reason}")
        typer.echo("브라우저에서 승인은 됐는데 여기서 못 받았다면 —")
        typer.echo("  주소창의 code= 값으로 `trader kakao-auth --code <값>` 을 실행하세요.")
        raise typer.Exit(1)

    _save_and_test(captured["code"])


@app.command("notify-test")
def notify_test(event: str = "notify.test") -> None:
    """설정된 알림 스택(콘솔+카카오+디스코드) 전체로 테스트 발송."""
    import asyncio
    from datetime import datetime

    from ..infra.notifier.factory import build_notifier

    s = _bootstrap()
    notifier = build_notifier(s)
    asyncio.run(
        notifier.notify(event, 채널점검="OK", 시각=datetime.now().strftime("%m-%d %H:%M"))
    )
    typer.echo("발송 시도 완료 — 콘솔 로그와 카카오톡을 확인하세요.")


@campaign_app.command("list")
def campaign_list() -> None:
    """List campaigns stored in the DB."""
    import asyncio

    from sqlalchemy import select

    from ..persistence.db import create_engine, session_factory, session_scope
    from ..persistence.models import Campaign

    s = _bootstrap()

    async def _list() -> list[Campaign]:
        engine = create_engine(s.db_url)
        try:
            async with session_scope(session_factory(engine)) as session:
                return list((await session.execute(select(Campaign))).scalars().all())
        finally:
            await engine.dispose()

    rows = asyncio.run(_list())
    if not rows:
        typer.echo("no campaigns yet — run one via CampaignManager (backtest) or the API.")
        return
    for c in rows:
        typer.echo(
            f"{c.campaign_id}  {c.name}  [{c.status}]  budget={c.initial_budget} {c.currency}  "
            f"{c.start_at:%Y-%m-%d} ~ {c.end_at:%Y-%m-%d}"
        )


if __name__ == "__main__":
    app()
