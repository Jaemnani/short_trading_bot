"""실순위 스냅샷 사후검증 (BACKLOG B) — 봇이 실제로 본 거래량순위로 스캐너를 재현.

기존 6개월 검증은 "야후 일봉으로 재구성한 근사 순위"였다. 이 스크립트는 가동 중
봇이 매 스캔 주기에 저장한 실제 순위 스냅샷(data/rankings/*.jsonl)으로
① 합류 판정을 재현해 실제 합류 기록과 대조하고 (판정 로직 충실도)
② 합류 종목을 당일 1분봉으로 리플레이해 손익을 계산한다 (실데이터 성적).

라이브와의 의도적 차이 (해석 시 유의):
- favorites(일봉 후보군 완화 문턱) 미재현 — 스냅샷에 기록되지 않음. 실제 합류가
  재현에서 빠지면 favorites 완화分인지 문턱 수치로 판별해 표시한다.
- 자본은 합류마다 고정 1,000만(공유현금·일손실 브레이크 없음), 주문캡 500만은 재현.
- 리플레이는 전략 설계대로 15:10 청산 — 봇 장애로 오버나이트된 실거래와 다를 수 있다.

사용: .venv/bin/python scripts/replay_rankings.py   (repo 루트에서; 캐시 없는 분봉만 KIS 조회)
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from short_trading_bot.app.watchlist import load_scanner_config, load_trading_config
from short_trading_bot.backtest.costs import CostModel
from short_trading_bot.domain.enums import Side
from short_trading_bot.domain.factory import PositionFactory
from short_trading_bot.domain.signal import IntentKind, Signal
from short_trading_bot.infra.config import get_settings
from short_trading_bot.market.indicators import IndicatorEngine
from short_trading_bot.market.kis_ranking import RankRow
from short_trading_bot.market.scanner import pick_momentum
from short_trading_bot.market.types import Bar, IndicatorSnapshot

KST = timezone(timedelta(hours=9))
RANKINGS = Path("data/rankings")
EQUITY = Decimal(10_000_000)
ORDER_CAP = Decimal(5_000_000)


@dataclass(slots=True)
class Join:
    day: date
    ts: str  # HH:MM:SS KST
    ticker: str
    name: str
    change_pct: float
    vol_surge: float


@dataclass(slots=True)
class Replayed:
    join: Join
    entered: bool
    net_pnl: Decimal
    exit_reason: str
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal


def replay_joins(cfg_path: str = "watchlist.json") -> list[Join]:
    """스냅샷을 시간순으로 돌며 라이브 스캔 루프와 같은 판정으로 합류를 재현."""
    scanner = load_scanner_config(cfg_path)
    watch, _ = load_trading_config(cfg_path)
    base_exclude = {key.split("@")[0] for key in watch}
    joins: list[Join] = []
    for f in sorted(RANKINGS.glob("*.jsonl")):
        day = datetime.strptime(f.stem, "%Y%m%d").date()
        today: list[Join] = []
        for line in f.read_text().splitlines():
            snap = json.loads(line)
            capacity = scanner.max_active - len(today)
            if capacity <= 0:
                break
            rows = [RankRow(**r) for r in snap["rows"]]
            picks = pick_momentum(
                rows,
                min_change_pct=scanner.min_change_pct,
                max_change_pct=scanner.max_change_pct,
                min_vol_surge=scanner.min_vol_surge,
                min_value=scanner.min_value_traded,
                exclude=base_exclude | {j.ticker for j in today},
                favorites=(),  # 재현 한계: 후보군 완화 미적용 (모듈 docstring)
                top=capacity,
            )
            today.extend(
                Join(day, snap["ts"], p.ticker, p.name, p.change_pct, p.vol_surge)
                for p in picks
            )
        joins.extend(today)
    return joins


async def _load_day_bars(ticker: str, day: date, *, fetch: bool = False) -> list[Bar]:
    """당일 1분봉 — 기본은 디스크 캐시 전용 (가동 중 봇과 KIS 토큰 발급 경합 방지:
    토큰 캐시가 프로세스 메모리라 스크립트가 새로 발급하면 분당 1회 제한에 걸린다).
    ``--fetch``일 때만 캐시 미스를 KIS에서 조회한다 (봇 정지 상태에서 권장)."""
    cache_file = Path("data/minutes") / ticker / f"{day:%Y%m%d}.json"
    if not fetch and not cache_file.exists():
        return []

    from short_trading_bot.app.engine import kis_rest_base
    from short_trading_bot.infra.kis_auth import KisAuth
    from short_trading_bot.market.kis_history import KisMinuteHistory

    s = get_settings()
    creds = s.active_kis()
    hist = KisMinuteHistory(KisAuth(creds, kis_rest_base(s.mode)), creds, kis_rest_base(s.mode))
    return await hist.fetch_day(ticker, day)


def replay_trade(join: Join, bars: list[Bar], cfg_path: str = "watchlist.json") -> Replayed:
    """합류 시각 이후를 라이브와 같은 경로(지표→랏 평가→비용 체결)로 1회전 리플레이.

    합류 이전 봉은 지표 워밍업으로만 사용 (라이브 prime()과 동일). one-shot이므로
    첫 랏이 종결되면 끝. 주문캡 500만은 ENTER 수량 축소로 재현 (라이브 자동 캡).
    """
    scanner = load_scanner_config(cfg_path)
    template = scanner.template()
    lot = PositionFactory.create(Signal(ticker=join.ticker), template)
    engine = IndicatorEngine()
    cost = CostModel()
    join_dt = datetime.combine(join.day, datetime.strptime(join.ts, "%H:%M:%S").time(), KST)

    prev: IndicatorSnapshot | None = None
    buy_notional = sell_notional = fees = tax = Decimal(0)
    entry_price = exit_price = qty_bought = Decimal(0)
    exit_reason = "no_entry"

    for bar in bars:
        snap = engine.update(bar)
        if bar.ts.astimezone(KST) < join_dt:
            prev = snap  # 합류 전 = 워밍업 (평가 없음)
            continue
        if lot.is_open:
            lot.on_bar(bar.high)
        for intent in lot.evaluate(snap, EQUITY, prev=prev, news_ewma=None):
            if not intent.is_actionable:
                continue
            if intent.side is Side.BUY:
                qty = intent.qty or Decimal(0)
                price = cost.buy_price(bar.close)
                if price * qty > ORDER_CAP:  # 라이브 주문캡 자동 축소 재현
                    qty = (ORDER_CAP / price).to_integral_value(rounding=ROUND_DOWN)
                if qty <= 0:
                    continue
                notional = price * qty
                buy_notional += notional
                fees += cost.fee(notional)
                qty_bought += qty
                entry_price = price
                lot.apply_fill(Side.BUY, qty, price, is_add=intent.kind is IntentKind.ADD)
            else:
                qty = lot.qty if intent.kind is IntentKind.EXIT else min(
                    intent.qty
                    or Decimal(str(intent.fraction or 0)) * qty_bought,
                    lot.qty,
                )
                if qty <= 0:
                    continue
                price = cost.sell_price(bar.close)
                notional = price * qty
                fee = cost.fee(notional)
                t = cost.sell_tax(notional, lot.market)
                sell_notional += notional
                fees += fee
                tax += t
                exit_price = price
                exit_reason = intent.reason or "closed"
                lot.apply_fill(Side.SELL, qty, price, fee, t)
        prev = snap
        if lot.is_terminal:
            break  # one-shot: 1회전 후 은퇴

    net = sell_notional - buy_notional - fees - tax if qty_bought > 0 else Decimal(0)
    return Replayed(
        join=join, entered=qty_bought > 0, net_pnl=net, exit_reason=exit_reason,
        entry_price=entry_price, exit_price=exit_price, qty=qty_bought,
    )


async def _replay_all(joins: list[Join], *, fetch: bool) -> list[Replayed]:
    results: list[Replayed] = []
    for j in joins:
        bars = await _load_day_bars(j.ticker, j.day, fetch=fetch)
        if not bars:
            print(f"{j.day} {j.ts} {j.ticker} {j.name}: 분봉 캐시 없음 — 건너뜀"
                  " (--fetch로 조회 가능, 봇 정지 상태 권장)")
            continue
        results.append(replay_trade(j, bars))
    return results


def main() -> None:
    import sys

    fetch = "--fetch" in sys.argv
    joins = replay_joins()
    days = len(list(RANKINGS.glob("*.jsonl")))
    print(f"— 재현 합류 {len(joins)}건 (스냅샷 {days}일치) —")
    results = asyncio.run(_replay_all(joins, fetch=fetch))

    print(f"\n{'날짜':<11}{'합류':<9}{'코드':<8}{'종목':<12}{'등락%':>6}{'체결':>5}"
          f"{'수량':>6}{'손익(원)':>10}  청산사유")
    total = Decimal(0)
    for r in results:
        j = r.join
        total += r.net_pnl
        print(f"{j.day!s:<11}{j.ts:<9}{j.ticker:<8}{j.name:<12}{j.change_pct:>6.1f}"
              f"{'O' if r.entered else '-':>5}{r.qty:>6}{r.net_pnl:>10,.0f}  {r.exit_reason}")
    entered = [r for r in results if r.entered]
    wins = sum(1 for r in entered if r.net_pnl > 0)
    print(f"\n합계 {total:+,.0f}원 | 체결 {len(entered)}/{len(results)} | "
          f"승률 {wins}/{len(entered) if entered else 0}")


if __name__ == "__main__":
    main()
