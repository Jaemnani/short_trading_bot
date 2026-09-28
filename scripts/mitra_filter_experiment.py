"""H-M1 오프라인 실험 — Mitra-v2 눌림목 1D 신호 필터 (BACKLOG.md "가설 사전등록 H-M1").

봇 본체는 건드리지 않는다. 라이브와 같은 ``IndicatorEngine``·``PullbackDaily`` 셋업 판정·
``Backtester``·``CostModel`` 을 그대로 쓰고, 실험 전용 전략(눌림목 + 진입 게이트)만 이 파일
안에서 등록한다. 설계·채택 기준은 사전등록 절이 원본이며 이 스크립트는 그 검증만 한다.

선견 차단 (워크포워드):
- 피처는 t 종가까지의 값만 (지표 엔진을 봉 순서대로 흘림). 코스피 피처도 같은 날짜까지.
- 라벨(t+N 종가 기준 순수익)은 t+N 에 확정된다 → 블록 시작일보다 **라벨 확정일이 앞선**
  표본만 지지 집합에 넣는다. 정규화(로지스틱)·전처리(Mitra)도 지지 집합으로만 맞춘다.

사용 (repo 루트, 순서대로 — iMac 권장. 데이터는 ``data/mitra/`` 에 캐시):
    .venv/bin/pip install finance-datareader "autogluon.tabular[mitra]==1.6.3"
    .venv/bin/python scripts/mitra_filter_experiment.py fetch                 # 유니버스 A
    .venv/bin/python scripts/mitra_filter_experiment.py fetch --delisted      # + 상폐(B), 오래 걸림
    .venv/bin/python scripts/mitra_filter_experiment.py predict --model logit --universe A
    .venv/bin/python scripts/mitra_filter_experiment.py predict --model logit --universe B --horizons 10
    .venv/bin/python scripts/mitra_filter_experiment.py predict --model mitra --universe A
    .venv/bin/python scripts/mitra_filter_experiment.py predict --model mitra --universe B --horizons 10
    .venv/bin/python scripts/mitra_filter_experiment.py latency
    .venv/bin/python scripts/mitra_filter_experiment.py evaluate              # → data/mitra/report.md

``predict`` 는 블록 단위로 이어쓰기 한다 (중단 후 재실행하면 이어서 진행).
평가 템플릿은 ``--watchlist``(기본 watchlist.json)에서 읽는다. 운용 기본값은 ``--watchlist none``.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Protocol

import numpy as np
from numpy.typing import NDArray

from short_trading_bot.backtest.costs import CostModel
from short_trading_bot.backtest.harness import Backtester, BacktestResult
from short_trading_bot.domain.enums import PositionState, Resolution, Side
from short_trading_bot.domain.factory import PositionFactory
from short_trading_bot.domain.signal import Intent, IntentKind, Signal
from short_trading_bot.market.indicators import IndicatorEngine
from short_trading_bot.market.types import Bar, IndicatorSnapshot
from short_trading_bot.strategy.algorithms.pullback_daily import PullbackDaily, PullbackParams
from short_trading_bot.strategy.base import StrategyContext, StrategyMeta
from short_trading_bot.strategy.registry import all_strategies, register_strategy
from short_trading_bot.strategy.templates import StrategyTemplate

FloatArr = NDArray[np.float64]

# -- 사전등록 고정값 (BACKLOG.md H-M1) — 바꾸려면 H-M2 로 새로 등록 -----------------------------

EVAL_TICKERS = ("005930", "000660", "005380")
EVAL_START = date(2016, 1, 1)
SUPPORT_START = date(2010, 1, 1)
HORIZONS = (5, 10, 20)
DELTAS = (-0.05, 0.0, 0.05)
CENTER = (10, 0.0)
REFRESH_DAYS = 5
MAX_SUPPORT = 5000
MIN_SUPPORT = 300
MIN_CLASS = 50
MIN_AVG_VALUE = 5e9  # 20일 평균 거래대금 50억
UNIVERSE_TOP = 100  # 지지 유니버스 A = KOSPI 시총 상위 100 보통주
STARTING_EQUITY = Decimal(10_000_000)
BEAR_WINDOWS = (
    ("2018", date(2018, 1, 1), date(2018, 12, 31)),
    ("2022", date(2022, 1, 1), date(2022, 12, 31)),
    ("2026-07", date(2026, 7, 1), date(2026, 8, 31)),
)
G0_MIN_AUC = 0.53
G0_MIN_REMOVED_PER_WINDOW = 10
G1_MIN_FILTERED = 5
G3_MAX_LATENCY_S = 60.0
MITRA_HF_MODEL = "autogluon/mitra-classifier-2"

# 운용 기본값 (watchlist.json 에 1D 눌림목 항목이 없을 때만 사용 — STRATEGIES/cli select-universe)
DEFAULT_TEMPLATE: dict[str, Any] = {
    "strategy_id": "pullback_daily_v1",
    "market": "KRX",
    "resolution": "1D",
    "risk_per_trade": 0.02,
    "strategy_params": {
        "rsi_min": 35, "rsi_max": 65, "touch_band_pct": 0.02, "bull_risk_mult": 2.0,
        "max_adds": 1, "require_above_sma120": True, "max_atr_pct": 0.05,
    },
}

FEATURES = (
    # 종목 (25)
    "c_sma20", "c_sma60", "c_sma120", "sma20_sma60", "sma60_sma120", "rsi14", "atr_pct",
    "macd_hist_pct", "bb_pctb", "bb_bw", "adx14", "di_diff", "stoch_k", "stoch_d", "rvol",
    "pos_high20", "pos_low20", "pull_depth", "ret1", "ret5", "ret20", "ret60", "vol_ratio",
    "log_value20", "c_high252",
    # 시장 (7)
    "ks_c_sma20", "ks_c_sma60", "ks_ret1", "ks_ret5", "ks_ret20", "ks_vol20", "rel_str20",
)
FEATURE_VERSION = 1

ROOT = Path("data/mitra")
BARS_DIR = ROOT / "bars"
PREDS_DIR = ROOT / "preds"
UNIVERSE_FILE = ROOT / "universe.json"
KS11 = "KS11"


# -- 데이터 ----------------------------------------------------------------------------------


@dataclass(slots=True)
class Series:
    """한 종목의 일봉 (날짜 오름차순)."""

    ticker: str
    days: list[date]
    open: FloatArr
    high: FloatArr
    low: FloatArr
    close: FloatArr
    volume: FloatArr

    def bars(self) -> list[Bar]:
        out: list[Bar] = []
        for i, d in enumerate(self.days):
            c = Decimal(str(self.close[i]))
            v = Decimal(str(int(self.volume[i])))
            out.append(Bar(
                self.ticker, Resolution.D1, datetime(d.year, d.month, d.day, tzinfo=UTC),
                Decimal(str(self.open[i])), Decimal(str(self.high[i])), Decimal(str(self.low[i])),
                c, v, c * v,
            ))
        return out


def save_series(s: Series, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for i, d in enumerate(s.days):
            w.writerow([d.isoformat(), s.open[i], s.high[i], s.low[i], s.close[i], s.volume[i]])


def load_series(ticker: str, path: Path) -> Series:
    days: list[date] = []
    cols: list[list[float]] = [[], [], [], [], []]
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            vals = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
            if vals[4] <= 0 or vals[3] <= 0 or not all(math.isfinite(v) for v in vals):
                continue  # 거래정지·결측 봉 제외 (라이브 백필과 같은 규칙)
            days.append(date.fromisoformat(row["date"]))
            for c, v in zip(cols, vals, strict=True):
                c.append(v)
    arr = [np.asarray(c, dtype=np.float64) for c in cols]
    return Series(ticker, days, arr[0], arr[1], arr[2], arr[3], arr[4])


def _frame_to_series(ticker: str, df: Any) -> Series:
    days: list[date] = []
    cols: list[list[float]] = [[], [], [], [], []]
    for idx, row in df.iterrows():
        try:
            vals = [float(row[k]) for k in ("Open", "High", "Low", "Close", "Volume")]
        except (KeyError, TypeError, ValueError):
            continue
        if vals[4] <= 0 or vals[3] <= 0 or not all(math.isfinite(v) for v in vals):
            continue
        days.append(date(idx.year, idx.month, idx.day))
        for c, v in zip(cols, vals, strict=True):
            c.append(v)
    arr = [np.asarray(c, dtype=np.float64) for c in cols]
    return Series(ticker, days, arr[0], arr[1], arr[2], arr[3], arr[4])


def check_fetch_resume(universe: dict[str, Any], refresh: bool, delisted: bool) -> None:
    """중단된 fetch 는 같은(또는 더 넓은) 범위로만 이어갈 수 있다.

    - ``--refresh`` 중단 → ``--refresh`` 필요 (일반 fetch 는 기존 파일을 건너뛰어 옛·새 일봉
      혼합을 '완료'로 승인하게 된다)
    - ``--delisted`` 중단 → ``--delisted`` 필요 (빠지면 상폐 일봉이 일부만 받아진 채 완료 처리)
    """
    if universe.get("fetch_complete") is not False:
        return
    if universe.get("fetch_mode") == "refresh" and not refresh:
        raise SystemExit("이전 `fetch --refresh` 가 중단됨 — 같은 `--refresh` 로 다시 실행")
    if universe.get("fetch_delisted") and not delisted:
        raise SystemExit("이전 `fetch --delisted` 가 중단됨 — 같은 `--delisted` 로 다시 실행")


def cmd_fetch(args: argparse.Namespace) -> None:
    import FinanceDataReader as fdr

    start = SUPPORT_START.isoformat()
    universe: dict[str, Any] = (
        json.loads(UNIVERSE_FILE.read_text()) if UNIVERSE_FILE.exists() else {}
    )
    check_fetch_resume(universe, args.refresh, args.delisted)
    if "top" not in universe or args.refresh_universe:
        listing = fdr.StockListing("KOSPI")
        if "Marcap" in listing.columns:
            listing = listing.sort_values("Marcap", ascending=False)
        top: list[str] = []
        for _, r in listing.iterrows():
            code = str(r["Code"])
            if code.endswith("0"):  # 보통주만 (우선주 코드는 5/7/9/K 로 끝남)
                top.append(code)
            if len(top) >= UNIVERSE_TOP:
                break
        universe.update({"created": date.today().isoformat(), "top": top})
    universe["eval"] = list(EVAL_TICKERS)
    if args.delisted and "delisted" not in universe:
        dl = fdr.StockListing("KRX-DELISTING")
        codes: list[str] = []
        for _, r in dl.iterrows():
            code, name = str(r.get("Symbol", "")), str(r.get("Name", ""))
            market = str(r.get("Market", ""))
            try:
                when = datetime.fromisoformat(str(r.get("DelistingDate"))[:10]).date()
            except ValueError:
                continue
            if (
                when >= SUPPORT_START and code.endswith("0") and len(code) == 6
                and (market == "KOSPI" or market.startswith("KOSDAQ"))
                and "스팩" not in name and str(r.get("SecuGroup", "주권")) == "주권"
            ):
                codes.append(code)
        universe["delisted"] = sorted(set(codes))
    ROOT.mkdir(parents=True, exist_ok=True)
    UNIVERSE_FILE.write_text(json.dumps(universe, ensure_ascii=False, indent=2))

    targets = [KS11, *universe["eval"], *universe["top"]]
    if args.delisted:
        targets += universe.get("delisted", [])
    # 진행 중 표시: 중단되면(예: --refresh 도중) 옛·새 일봉이 섞인 캐시를 후속 명령이 거부한다.
    universe["fetch_complete"] = False
    universe["fetch_mode"] = "refresh" if args.refresh else "normal"
    universe["fetch_delisted"] = bool(args.delisted)
    UNIVERSE_FILE.write_text(json.dumps(universe, ensure_ascii=False, indent=2))
    failed: list[str] = []
    for n, code in enumerate(dict.fromkeys(targets), 1):
        path = BARS_DIR / f"{code}.csv"
        if _has_bars(path) and not args.refresh:  # 헤더만 남은 파일은 다시 받는다
            continue
        df = None
        for symbol in (code, f"KRX-DELISTING:{code}"):
            try:
                df = fdr.DataReader(symbol, start)
            except Exception:
                df = None
            if df is not None and len(df) > 0:
                break
        series = _frame_to_series(code, df) if df is not None and len(df) > 0 else None
        if series is None or not series.days:  # 원본이 비었거나 정규화 후 유효 봉 0개
            failed.append(code)
            path.unlink(missing_ok=True)  # 새로고침 실패 → 옛 일봉을 남기지 않는다 (섞임 방지)
            continue
        save_series(series, path)
        if n % 50 == 0:
            print(f"  {n}/{len(targets)} …", flush=True)
    # 조회 불가로 인정하는 것은 상폐 종목뿐 (유니버스 B 에서 명시적으로 제외·보고). 그 외 실패는
    # predict 가 거부한다 — 조용히 빠진 종목으로 '완전한' 예측이 만들어지지 않게.
    delisted = set(universe.get("delisted", []))
    if args.delisted:  # 상폐 종목을 실제로 조회한 실행만 '조회 불가' 목록을 갱신한다
        universe["unavailable"] = sorted(c for c in delisted if not _has_bars(BARS_DIR / f"{c}.csv"))
    UNIVERSE_FILE.write_text(json.dumps(universe, ensure_ascii=False, indent=2))
    required_failed = [c for c in failed if c not in delisted]
    universe["required_failed"] = required_failed
    universe["fetch_complete"] = True
    UNIVERSE_FILE.write_text(json.dumps(universe, ensure_ascii=False, indent=2))
    print(f"fetch 완료: 대상 {len(targets)}, 실패 {len(failed)} {failed[:10]}")
    if required_failed:
        print(f"⚠️ 필수 종목(지수·평가·시총 상위) 실패 {required_failed} — 재실행 전엔 predict 불가")
    if args.delisted and universe["unavailable"]:
        print(f"상폐 {len(delisted)}종목 중 조회 불가 {len(universe['unavailable'])} — B 에서 제외")


# -- 템플릿 ----------------------------------------------------------------------------------


def load_template(watchlist: Path | None) -> tuple[dict[str, Any], str]:
    """평가 3종목의 1D 눌림목 템플릿 (Backtester 는 템플릿 1개 — 셋 다 같아야 한다).

    - 운용 기본값은 ``--watchlist none``(``watchlist`` = None)으로 **명시 선택**할 때만 쓴다.
      파일이 없으면 SystemExit — 운용 설정과 다른 실험이 조용히 채점되지 않게.
    - 파일이 있으면 세 종목 **모두** 1D 눌림목 항목이 있고 서로 같아야 한다. 일부만 있거나
      다르면 SystemExit — 한 종목 설정을 다른 종목에 조용히 덮어씌우지 않는다.
    """
    if watchlist is None:
        return dict(DEFAULT_TEMPLATE), "DEFAULT_TEMPLATE (--watchlist none 명시)"
    if not watchlist.exists():
        raise SystemExit(
            f"{watchlist} 없음 — 운용 워치리스트 경로를 주거나 `--watchlist none` 으로 운용 기본값을 "
            "명시 선택"
        )
    data = json.loads(watchlist.read_text(encoding="utf-8")).get("watchlist", {})
    by_ticker: dict[str, list[dict[str, Any]]] = {t: [] for t in EVAL_TICKERS}
    for key, cfg in data.items():
        ticker = key.split("@")[0]
        if (
            ticker in by_ticker and cfg.get("strategy_id") == "pullback_daily_v1"
            and cfg.get("resolution", "1D") == "1D"
        ):
            by_ticker[ticker].append(cfg)
    missing = [t for t, cfgs in by_ticker.items() if not cfgs]
    if missing:
        raise SystemExit(
            f"{watchlist}: 평가 종목 {missing} 의 1D 눌림목 항목 없음 — 워치리스트를 맞추거나 "
            "`--watchlist none` 으로 운용 기본값을 명시 선택"
        )
    blobs = {json.dumps(c, sort_keys=True) for cfgs in by_ticker.values() for c in cfgs}
    if len(blobs) != 1:
        raise SystemExit(f"{watchlist}: 평가 3종목의 1D 눌림목 템플릿이 서로 다름 — 사전등록 전제 위반")
    return dict(by_ticker[EVAL_TICKERS[0]][0]), str(watchlist)


def _watchlist_arg(value: str) -> Path | None:
    return None if value.lower() == "none" else Path(value)


def cost_key() -> list[float]:
    """라벨(순수익)과 백테스트가 같이 쓰는 비용 모델 파라미터 — 캐시 키에 들어간다."""
    c = CostModel()
    return [c.fee_bps, c.sell_tax_bps, c.slippage_bps]


def template_hash(template: dict[str, Any]) -> str:
    blob = json.dumps(
        {"t": template, "fv": FEATURE_VERSION, "h": HORIZONS, "liq": MIN_AVG_VALUE,
         "cost": cost_key()},
        sort_keys=True,
    )
    return hashlib.sha1(blob.encode()).hexdigest()[:10]


def file_sha(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


CHECKPOINT_FILES = ("config.json", "model.safetensors")


def resolve_checkpoint(hf_model: str) -> tuple[str, str]:
    """Mitra 체크포인트를 로컬 스냅샷 하나로 고정하고 (디렉터리, 내용 해시) 를 돌려준다.

    예측·지연 측정은 반환된 디렉터리로만 모델을 적재하고, 지문·메타데이터에는 내용 해시를 쓴다
    — 같은 이름으로 가중치가 바뀌어도(로컬 교체·HF 저장소 갱신) 옛 예측과 섞이지 않는다."""
    path = Path(hf_model)
    if not path.is_dir():
        try:
            from huggingface_hub import snapshot_download

            path = Path(snapshot_download(hf_model, allow_patterns=list(CHECKPOINT_FILES)))
        except Exception as e:
            raise SystemExit(f"Mitra 체크포인트 `{hf_model}` 확인 불가: {e}") from e
    absent = [f for f in CHECKPOINT_FILES if not (path / f).is_file()]
    if absent:
        raise SystemExit(f"Mitra 체크포인트 `{hf_model}` 에 {absent} 없음")
    h = hashlib.sha1()
    for name in CHECKPOINT_FILES:
        h.update(f"{name}:{file_sha(path / name)};".encode())
    return str(path), h.hexdigest()[:16]


def experiment_fingerprint(
    model: str, universe: str, template: dict[str, Any], tickers: Sequence[str],
    checkpoint: str | None,
) -> str:
    """예측 캐시 키: 실험 구성 전체 + 일봉 캐시 내용. 하나라도 바뀌면 다른 파일로 새로 예측한다."""
    bars = {t: file_sha(BARS_DIR / f"{t}.csv") for t in (KS11, *tickers)
            if (BARS_DIR / f"{t}.csv").exists()}
    blob = {
        "model": model, "universe": universe, "tickers": list(tickers), "template": template,
        "fv": FEATURE_VERSION, "checkpoint": checkpoint if model == "mitra" else None,
        "bars": bars,
        "fixed": [EVAL_START.isoformat(), SUPPORT_START.isoformat(), REFRESH_DAYS, MAX_SUPPORT,
                  MIN_SUPPORT, MIN_CLASS, MIN_AVG_VALUE, list(EVAL_TICKERS), UNIVERSE_TOP],
        "cost": cost_key(),
    }
    return hashlib.sha1(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:12]


# -- 피처·표본 --------------------------------------------------------------------------------


@dataclass(slots=True)
class Sample:
    ticker: str
    day: date
    x: tuple[float, ...]
    liquid: bool
    fwd: dict[int, tuple[date, float]] = field(default_factory=dict)  # N -> (라벨 확정일, 순수익)


def net_return(entry_close: float, exit_close: float, cost: CostModel) -> float:
    """CostModel 과 같은 규칙: 매수 슬리피지+수수료, 매도 슬리피지+수수료+거래세."""
    slip = cost.slippage_bps / 1e4
    fee = cost.fee_bps / 1e4
    tax = cost.sell_tax_bps / 1e4
    buy = entry_close * (1 + slip) * (1 + fee)
    sell = exit_close * (1 - slip) * (1 - fee - tax)
    return sell / buy - 1.0


def market_features(ks: Series) -> dict[date, tuple[float, ...]]:
    c = ks.close
    out: dict[date, tuple[float, ...]] = {}
    rets = np.diff(c) / c[:-1]
    for i, d in enumerate(ks.days):
        if i < 60:
            continue
        sma20 = float(c[i - 19 : i + 1].mean())
        sma60 = float(c[i - 59 : i + 1].mean())
        out[d] = (
            c[i] / sma20 - 1, c[i] / sma60 - 1,
            c[i] / c[i - 1] - 1, c[i] / c[i - 5] - 1, c[i] / c[i - 20] - 1,
            float(rets[i - 20 : i].std(ddof=0)),
        )
    return out


def _ratio(a: float | None, b: float | None) -> float:
    if a is None or b is None or b == 0:
        return math.nan
    return a / b - 1.0


def _val(v: float | None) -> float:
    return math.nan if v is None else float(v)


def stock_features(
    snap: IndicatorSnapshot, s: Series, i: int, mkt: tuple[float, ...] | None
) -> tuple[float, ...]:
    """사전등록 32피처. 전부 i(=t) 봉까지의 값."""
    close = float(snap.close)
    g = snap.get
    c, v = s.close, s.volume

    def ret(k: int) -> float:
        return float(c[i] / c[i - k] - 1) if i >= k else math.nan

    value20 = float((c[max(0, i - 19) : i + 1] * v[max(0, i - 19) : i + 1]).mean())
    hi252 = float(s.high[max(0, i - 251) : i + 1].max()) if i >= 251 else math.nan
    pdi, mdi = g("plus_di"), g("minus_di")
    atr, macd_hist = g("atr_14"), g("macd_hist")
    stock = (
        _ratio(close, g("sma_20")), _ratio(close, g("sma_60")), _ratio(close, g("sma_120")),
        _ratio(g("sma_20"), g("sma_60")), _ratio(g("sma_60"), g("sma_120")),
        _val(g("rsi_14")),
        atr / close if atr is not None and close > 0 else math.nan,
        macd_hist / close if macd_hist is not None and close > 0 else math.nan,
        _val(g("bb_pctb")), _val(g("bb_bw")), _val(g("adx_14")),
        pdi - mdi if pdi is not None and mdi is not None else math.nan,
        _val(g("stoch_k")), _val(g("stoch_d")), _val(g("rvol")),
        _ratio(close, g("high_20")), _ratio(close, g("low_20")),
        _ratio(float(snap.low), g("sma_20")),
        ret(1), ret(5), ret(20), ret(60),
        float(v[i] / v[i - 1]) if i >= 1 and v[i - 1] > 0 else math.nan,
        math.log10(value20) if value20 > 0 else math.nan,
        close / hi252 - 1 if math.isfinite(hi252) and hi252 > 0 else math.nan,
    )
    market = mkt if mkt is not None else (math.nan,) * 6
    return (*stock, *market, ret(20) - market[4])


def build_samples(
    s: Series,
    mkt: dict[date, tuple[float, ...]],
    template: dict[str, Any],
    cost: CostModel,
    horizons: Sequence[int] = HORIZONS,
) -> list[Sample]:
    """셋업 성립 봉(``PullbackDaily._setup_fail is None``)마다 표본 1개.

    지표는 라이브와 같은 ``IndicatorEngine`` 에 봉 순서대로 흘려 계산한다 (선견 없음).
    피처에 결측이 있는 봉(워밍업 등)은 버린다.
    """
    tmpl = StrategyTemplate.model_validate(template)
    lot = PositionFactory.create(Signal(ticker=s.ticker, market=tmpl.market), tmpl)
    strategy = lot.strategy
    assert isinstance(strategy, PullbackDaily)
    params = strategy.params
    assert isinstance(params, PullbackParams)
    engine = IndicatorEngine()
    prev: IndicatorSnapshot | None = None
    out: list[Sample] = []
    for i, bar in enumerate(s.bars()):
        snap = engine.update(bar)
        if snap.bar_count >= strategy.warmup_bars:
            ctx = StrategyContext(
                snapshot=snap, state=PositionState.WATCHING, qty=Decimal(0),
                avg_entry=Decimal(0), peak_price=snap.close, bars_held=0,
                params=lot.params, equity=STARTING_EQUITY, prev=prev,
            )
            if strategy._setup_fail(ctx, params) is None:
                x = stock_features(snap, s, i, mkt.get(s.days[i]))
                if all(math.isfinite(f) for f in x):
                    value20 = float(
                        (s.close[max(0, i - 19) : i + 1] * s.volume[max(0, i - 19) : i + 1]).mean()
                    )
                    smp = Sample(s.ticker, s.days[i], x, value20 >= MIN_AVG_VALUE)
                    for n in horizons:
                        if i + n < len(s.days):
                            smp.fwd[n] = (
                                s.days[i + n], net_return(s.close[i], s.close[i + n], cost),
                            )
                    out.append(smp)
        prev = snap
    return out


def _samples_path(cache: Path, ticker: str) -> Path:
    return cache / f"{ticker}.json"


def _build_one(job: tuple[str, str, str, dict[str, Any]]) -> tuple[str, int]:
    ticker, bars_path, cache_dir, template = job
    ks = load_series(KS11, BARS_DIR / f"{KS11}.csv")
    samples = build_samples(load_series(ticker, Path(bars_path)), market_features(ks), template,
                            CostModel())
    rows = [
        {"d": smp.day.isoformat(), "x": list(smp.x), "l": smp.liquid,
         "f": {str(n): [ld.isoformat(), r] for n, (ld, r) in smp.fwd.items()}}
        for smp in samples
    ]
    payload = {"bars_sha": file_sha(Path(bars_path)), "ks_sha": file_sha(BARS_DIR / f"{KS11}.csv"),
               "rows": rows}
    _samples_path(Path(cache_dir), ticker).write_text(json.dumps(payload))
    return ticker, len(rows)


def _cache_fresh(path: Path, bars: Path) -> bool:
    if not path.exists():
        return False
    try:
        cached = json.loads(path.read_text())
    except ValueError:
        return False
    return (
        isinstance(cached, dict)
        and cached.get("bars_sha") == file_sha(bars)
        and cached.get("ks_sha") == file_sha(BARS_DIR / f"{KS11}.csv")  # 시장 피처 원천
    )


def load_samples(tickers: Iterable[str], template: dict[str, Any], jobs: int) -> list[Sample]:
    cache = ROOT / "samples" / template_hash(template)
    cache.mkdir(parents=True, exist_ok=True)
    todo = [
        (t, str(BARS_DIR / f"{t}.csv"), str(cache), template)
        for t in tickers
        if (BARS_DIR / f"{t}.csv").exists()
        and not _cache_fresh(_samples_path(cache, t), BARS_DIR / f"{t}.csv")
    ]
    if todo:
        print(f"표본 생성: {len(todo)}종목 (jobs={jobs}) …", flush=True)
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for n, (t, k) in enumerate(pool.map(_build_one, todo), 1):
                if n % 25 == 0 or n == len(todo):
                    print(f"  {n}/{len(todo)} ({t}: {k})", flush=True)
    out: list[Sample] = []
    for t in tickers:
        path = _samples_path(cache, t)
        if not path.exists():
            continue
        for r in json.loads(path.read_text())["rows"]:
            out.append(Sample(
                t, date.fromisoformat(r["d"]), tuple(r["x"]), bool(r["l"]),
                {int(n): (date.fromisoformat(ld), float(ret)) for n, (ld, ret) in r["f"].items()},
            ))
    return out


# -- 예측기 ----------------------------------------------------------------------------------


class Predictor(Protocol):
    name: str

    def predict(self, xs: FloatArr, ys: NDArray[np.int64], xq: FloatArr) -> FloatArr:
        """지지 집합(xs, ys)만 보고 질의 xq 의 P(y=1) 을 돌려준다."""
        ...


class LogitPredictor:
    """대조군: 지지 집합으로 표준화 + L2 로지스틱 회귀 (뉴턴법)."""

    name = "logit"

    def __init__(self, l2: float = 1.0, iters: int = 25) -> None:
        self._l2 = l2
        self._iters = iters

    def predict(self, xs: FloatArr, ys: NDArray[np.int64], xq: FloatArr) -> FloatArr:
        mu = xs.mean(axis=0)
        sd = xs.std(axis=0)
        sd[sd == 0] = 1.0
        a = np.clip((xs - mu) / sd, -5, 5)
        q = np.clip((xq - mu) / sd, -5, 5)
        a = np.hstack([np.ones((len(a), 1)), a])
        q = np.hstack([np.ones((len(q), 1)), q])
        y = ys.astype(np.float64)
        w = np.zeros(a.shape[1])
        reg = np.eye(a.shape[1]) * self._l2
        reg[0, 0] = 0.0  # 절편은 규제하지 않음
        for _ in range(self._iters):
            p = 1.0 / (1.0 + np.exp(-(a @ w)))
            grad = a.T @ (p - y) + reg @ w
            hess = (a * (p * (1 - p))[:, None]).T @ a + reg
            step = np.linalg.solve(hess, grad)
            w -= step
            if float(np.abs(step).max()) < 1e-8:
                break
        out: FloatArr = 1.0 / (1.0 + np.exp(-(q @ w)))
        return out


class MitraPredictor:
    """Mitra-v2 in-context 예측 (파인튜닝 없음). 블록마다 지지 집합을 새로 넣는다.

    기본은 빠른 경로: 가중치를 한 번만 적재하고, 공개 ``fit()`` 이 파인튜닝 없이도 도는 검증
    패스(지지 집합 인코딩 1회 추가)를 건너뛴다 — 예측값은 공개 경로와 같다(테스트로 대조).
    AutoGluon 내부 API 에 기대므로 버전이 바뀌어 깨지면 ``fast=False``(공개 경로)로 돌린다.
    """

    name = "mitra"

    def __init__(
        self, hf_model: str = MITRA_HF_MODEL, device: str = "auto", *, fast: bool = True
    ) -> None:
        from autogluon.tabular.models.mitra.sklearn_interface import MitraClassifier

        kwargs: dict[str, Any] = {"hf_model": hf_model, "fine_tune": False, "verbose": False}
        if device != "auto":
            kwargs["device"] = device
        self._kwargs = kwargs
        self._cls = MitraClassifier
        self.device = str(MitraClassifier(**kwargs).device)  # 'auto' 가 실제로 고른 장치
        self.fast = fast
        self._fast: tuple[Any, Any, str] | None = None
        if fast:
            from autogluon.tabular.models.mitra.sklearn_interface import DEFAULT_CLASSES

            clf = MitraClassifier(**kwargs)
            cfg, tab2d = clf._create_config(clf.task, DEFAULT_CLASSES)
            self._fast = (cfg, tab2d.from_pretrained(hf_model, device=clf.device), clf.device)

    def predict(self, xs: FloatArr, ys: NDArray[np.int64], xq: FloatArr) -> FloatArr:
        if self._fast is None:
            clf = self._cls(**self._kwargs)
            k = max(16, min(64, len(xs) // 10))
            # X_val 을 명시해 지지 집합이 학습/검증으로 쪼개지지 않게 한다 (파인튜닝 없음)
            clf.fit(xs, ys, X_val=xs[-k:], y_val=ys[-k:])
            proba = np.asarray(clf.predict_proba(xq), dtype=np.float64)
        else:
            proba = self._predict_fast(xs, ys, xq)
        out: FloatArr = proba[:, 1]
        return out

    def _predict_fast(self, xs: FloatArr, ys: NDArray[np.int64], xq: FloatArr) -> FloatArr:
        from autogluon.common.utils.random import get_numpy_seed
        from autogluon.tabular.models.mitra._internal.core.trainer_finetune import TrainerFinetune
        from autogluon.tabular.models.mitra.sklearn_interface import (
            DEFAULT_CLASSES,
            mitra_deterministic_context,
        )

        assert self._fast is not None
        cfg, model, device = self._fast
        with mitra_deterministic_context():
            trainer = TrainerFinetune(
                cfg, model, n_classes=DEFAULT_CLASSES, device=device,
                rng=np.random.RandomState(get_numpy_seed(cfg.seed)), verbose=False,
            )
            trainer.preprocessor.fit(xs, ys)  # 공개 fit() 의 train() 첫 단계와 동일
            logits = trainer.predict(xs, ys, xq)[..., : len(np.unique(ys))]
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        proba: FloatArr = np.asarray(e / e.sum(axis=1, keepdims=True), dtype=np.float64)
        return proba


# -- 워크포워드 --------------------------------------------------------------------------------


@dataclass(slots=True)
class Pred:
    ticker: str
    day: date
    block: date
    p: float
    base: float
    n_support: int
    label: int | None
    net_ret: float | None


def block_starts(calendar: Sequence[date], start: date, every: int) -> list[date]:
    days = [d for d in calendar if d >= start]
    return days[::every]


@dataclass(slots=True)
class BlockPlan:
    """예측 대상 블록 하나: 지지 구간 [lo, hi) (라벨 확정일 순 정렬 인덱스) + 질의."""

    block: date
    lo: int
    hi: int
    queries: list[Sample]


@dataclass(slots=True)
class Support:
    xs: FloatArr
    ys: NDArray[np.int64]


def plan_blocks(
    pool: Sequence[Sample],
    queries: Sequence[Sample],
    horizon: int,
    blocks: Sequence[date],
    *,
    max_support: int = MAX_SUPPORT,
    min_support: int = MIN_SUPPORT,
    min_class: int = MIN_CLASS,
) -> tuple[Support, list[BlockPlan]]:
    """블록 [b_k, b_{k+1}) 의 질의와, '라벨 확정일 < b_k' 인 최근 지지 표본 구간.

    지지가 얇은 블록(행 < min_support 또는 한 클래스 < min_class)은 계획에서 빠진다 —
    예측 없음 = 게이트 통과(현행 동작)."""
    labeled = sorted((s for s in pool if horizon in s.fwd), key=lambda s: s.fwd[horizon][0])
    ys_all = np.asarray([1 if s.fwd[horizon][1] > 0 else 0 for s in labeled], dtype=np.int64)
    support = Support(np.asarray([s.x for s in labeled], dtype=np.float64), ys_all)
    if not labeled:
        return support, []
    label_days = [s.fwd[horizon][0] for s in labeled]
    by_block: dict[date, list[Sample]] = {}
    for q in queries:
        k = bisect.bisect_right(blocks, q.day) - 1
        if k >= 0:
            by_block.setdefault(blocks[k], []).append(q)
    plans: list[BlockPlan] = []
    for b in blocks:
        qs = by_block.get(b)
        if not qs:
            continue
        hi = bisect.bisect_left(label_days, b)  # 라벨 확정일 < b 만 (엄격)
        lo = max(0, hi - max_support)
        pos = int(ys_all[lo:hi].sum())
        if hi - lo < min_support or pos < min_class or (hi - lo) - pos < min_class:
            continue
        plans.append(BlockPlan(b, lo, hi, qs))
    return support, plans


def walk_forward(
    pool: Sequence[Sample],
    queries: Sequence[Sample],
    horizon: int,
    predictor: Predictor,
    blocks: Sequence[date],
    *,
    done: set[date] | None = None,
    on_block: Callable[[list[Pred]], None] | None = None,
    max_support: int = MAX_SUPPORT,
    min_support: int = MIN_SUPPORT,
    min_class: int = MIN_CLASS,
) -> list[Pred]:
    """블록 [b_k, b_{k+1}) 의 질의를 '라벨 확정일 < b_k' 인 지지 표본만으로 예측한다."""
    support, plans = plan_blocks(pool, queries, horizon, blocks, max_support=max_support,
                                 min_support=min_support, min_class=min_class)
    out: list[Pred] = []
    for plan in plans:
        if done is not None and plan.block in done:
            continue
        ys = support.ys[plan.lo : plan.hi]
        xq = np.asarray([q.x for q in plan.queries], dtype=np.float64)
        p = predictor.predict(support.xs[plan.lo : plan.hi], ys, xq)
        base = float(ys.mean())
        rows = [
            Pred(
                q.ticker, q.day, plan.block, float(p[j]), base, plan.hi - plan.lo,
                (1 if q.fwd[horizon][1] > 0 else 0) if horizon in q.fwd else None,
                q.fwd[horizon][1] if horizon in q.fwd else None,
            )
            for j, q in enumerate(plan.queries)
        ]
        out += rows
        if on_block is not None:
            on_block(rows)
    return out


def expected_keys(plans: Sequence[BlockPlan]) -> dict[date, set[tuple[str, date]]]:
    return {p.block: {(q.ticker, q.day) for q in p.queries} for p in plans}


def complete_blocks(
    rows: Sequence[Pred], expected: dict[date, set[tuple[str, date]]]
) -> set[date]:
    """질의 키가 계획과 정확히 일치(누락·중복·초과 없음)하는 블록만 완료로 본다."""
    got: dict[date, list[tuple[str, date]]] = {}
    for r in rows:
        got.setdefault(r.block, []).append((r.ticker, r.day))
    return {
        b for b, keys in got.items()
        if b in expected and len(keys) == len(set(keys)) and set(keys) == expected[b]
    }


def is_complete(rows: Sequence[Pred], expected: dict[date, set[tuple[str, date]]]) -> bool:
    return complete_blocks(rows, expected) == set(expected) and {r.block for r in rows} <= set(
        expected
    )


PRED_FIELDS = ("ticker", "day", "block", "p", "base", "n_support", "label", "net_ret")


def preds_path(model: str, universe: str, horizon: int, fingerprint: str) -> Path:
    return PREDS_DIR / f"{model}_{universe}_N{horizon}_{fingerprint}.csv"


def marker_path(path: Path) -> Path:
    return path.with_name(path.stem + ".done.json")


def run_path(path: Path) -> Path:
    return path.with_name(path.stem + ".run.json")


def claim_run(path: Path, run: dict[str, Any]) -> None:
    """예측 파일의 실행 경로(장치·빠른/공개 경로)를 처음 만들 때 고정한다.

    다른 경로로 이어 쓰거나 재사용하면 SystemExit — 완료 표시의 실행 경로가 CSV 를 실제로
    만든 경로임을 보장한다 (G3 대조의 전제)."""
    rp = run_path(path)
    if rp.exists():
        prev = json.loads(rp.read_text())
        if prev != run:
            raise SystemExit(
                f"{path.name}: 기존 예측은 실행 경로 {prev} 로 만들어짐 (지금 {run}) — 같은 "
                "--device/--slow 로 재개하거나 해당 예측 파일들을 지우고 처음부터"
            )
        return
    if path.exists():
        raise SystemExit(f"{path.name}: 실행 경로 기록 없는 예측 파일 — 지우고 다시 실행")
    path.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(run))


def _rewrite_preds(path: Path, rows: list[Pred]) -> None:
    path.unlink(missing_ok=True)
    if rows:
        _append_preds(path, rows)


def read_preds(path: Path) -> list[Pred]:
    if not path.exists():
        return []
    out: list[Pred] = []
    with path.open(newline="") as f:
        for r in csv.DictReader(f):
            out.append(Pred(
                r["ticker"], date.fromisoformat(r["day"]), date.fromisoformat(r["block"]),
                float(r["p"]), float(r["base"]), int(r["n_support"]),
                int(r["label"]) if r["label"] != "" else None,
                float(r["net_ret"]) if r["net_ret"] != "" else None,
            ))
    return out


def _append_preds(path: Path, rows: list[Pred]) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(PRED_FIELDS)
        for r in rows:
            w.writerow([r.ticker, r.day.isoformat(), r.block.isoformat(), r.p, r.base,
                        r.n_support, "" if r.label is None else r.label,
                        "" if r.net_ret is None else r.net_ret])


def universe_tickers(universe: str) -> list[str]:
    """선언된 유니버스 구성 종목. B 는 fetch 가 '조회 불가'로 기록한 상폐 종목만 제외한다.

    일봉이 하나라도 없으면 SystemExit — 누락 종목을 조용히 뺀 채 완전하다고 채점하지 않는다."""
    u = json.loads(UNIVERSE_FILE.read_text())
    if not u.get("fetch_complete", False):
        raise SystemExit("fetch 가 완료되지 않음(중단된 새로고침 등) — fetch 재실행 필요")
    if u.get("required_failed"):
        raise SystemExit(f"필수 종목 조회 실패 {u['required_failed']} — fetch 재실행 필요")
    if len(u["top"]) != UNIVERSE_TOP or list(u["eval"]) != list(EVAL_TICKERS):
        raise SystemExit(
            f"유니버스가 사전등록(시총 상위 {UNIVERSE_TOP} + 평가 {list(EVAL_TICKERS)})과 다름 — "
            "`fetch --refresh-universe` 로 다시 만들 것"
        )
    tickers = [*u["eval"], *u["top"]]
    if universe == "B":
        if "delisted" not in u:
            raise SystemExit("유니버스 B 는 먼저 `fetch --delisted` 필요")
        unavailable = set(u.get("unavailable", []))
        tickers += [c for c in u["delisted"] if c not in unavailable]
    tickers = list(dict.fromkeys(tickers))
    missing = [t for t in (KS11, *tickers) if not _has_bars(BARS_DIR / f"{t}.csv")]
    if missing:
        raise SystemExit(
            f"유니버스 {universe}: 일봉 없는 종목 {len(missing)}개 {missing[:10]} — fetch 재실행 필요"
        )
    return tickers


def _has_bars(path: Path) -> bool:
    """헤더만 있는 CSV 는 일봉 없음으로 본다."""
    if not path.exists():
        return False
    with path.open() as f:
        return sum(1 for _ in zip(range(2), f, strict=False)) >= 2


def unavailable_note() -> str:
    u = json.loads(UNIVERSE_FILE.read_text()) if UNIVERSE_FILE.exists() else {}
    return (f"상폐 {len(u.get('delisted', []))}종목 중 조회 불가 {len(u.get('unavailable', []))}"
            "종목 제외")


def split_pool_queries(
    samples: Sequence[Sample], eval_tickers: Sequence[str], eval_start: date
) -> tuple[list[Sample], list[Sample]]:
    """지지 풀 = 유동성 통과 표본(SUPPORT_START 이후). 질의 = 평가 구간의 풀 표본 + 평가 종목 셋업."""
    def in_pool(s: Sample) -> bool:
        return s.liquid and s.day >= SUPPORT_START

    pool = [s for s in samples if in_pool(s)]
    seen: set[tuple[str, date]] = set()
    queries: list[Sample] = []
    for s in samples:
        if s.day < eval_start or not (in_pool(s) or s.ticker in eval_tickers):
            continue
        if (s.ticker, s.day) not in seen:
            seen.add((s.ticker, s.day))
            queries.append(s)
    return pool, queries


def cmd_predict(args: argparse.Namespace) -> None:
    template, source = load_template(_watchlist_arg(args.watchlist))
    print(f"템플릿: {source}")
    tickers = universe_tickers(args.universe)
    samples = load_samples(tickers, template, args.jobs)
    pool, queries = split_pool_queries(samples, EVAL_TICKERS, EVAL_START)
    ks = load_series(KS11, BARS_DIR / f"{KS11}.csv")
    blocks = block_starts(ks.days, EVAL_START, REFRESH_DAYS)
    print(f"표본 {len(samples)} · 지지 풀 {len(pool)} · 질의 {len(queries)} · 블록 {len(blocks)}")
    checkpoint: str | None = None
    predictor: Predictor = LogitPredictor()
    if args.model == "mitra":
        ckpt_dir, checkpoint = resolve_checkpoint(args.hf_model)
        print(f"Mitra 체크포인트: {args.hf_model} → {ckpt_dir} (내용 해시 {checkpoint})")
        predictor = MitraPredictor(ckpt_dir, args.device, fast=not args.slow)
    fp = experiment_fingerprint(args.model, args.universe, template, tickers, checkpoint)
    run: dict[str, Any] = {}
    if isinstance(predictor, MitraPredictor):
        run = {"device": predictor.device, "fast": predictor.fast}
    for n in (int(h) for h in args.horizons.split(",")):
        path = preds_path(args.model, args.universe, n, fp)
        claim_run(path, run)
        marker_path(path).unlink(missing_ok=True)
        expected = expected_keys(plan_blocks(pool, queries, n, blocks)[1])
        existing = read_preds(path)
        done = complete_blocks(existing, expected)
        kept = [r for r in existing if r.block in done]
        if len(kept) != len(existing):  # 중단으로 반쯤 쓰인 블록 등은 버리고 다시 예측
            print(f"  N={n}: 불완전 행 {len(existing) - len(kept)}개 폐기 후 이어서 진행")
            _rewrite_preds(path, kept)
        t0 = time.time()
        count = [0]

        def on_block(rows: list[Pred], path: Path = path, t0: float = t0,
                     count: list[int] = count, n: int = n) -> None:
            _append_preds(path, rows)
            count[0] += 1
            if count[0] % 20 == 0:
                print(f"  N={n}: 블록 {count[0]} ({time.time() - t0:.0f}s)", flush=True)

        walk_forward(pool, queries, n, predictor, blocks, done=done, on_block=on_block)
        final = read_preds(path)
        complete = is_complete(final, expected)
        marker_path(path).write_text(json.dumps({
            "fingerprint": fp, "complete": complete, "blocks": len(expected),
            "queries": sum(len(v) for v in expected.values()), "rows": len(final), "run": run,
        }))
        state = "완료" if complete else "불완전 — 다시 실행하면 이어서 진행"
        print(f"N={n} {state} → {path} ({time.time() - t0:.0f}s)")


def load_complete_preds(
    model: str, universe: str, horizon: int, template: dict[str, Any], checkpoint: str | None
) -> tuple[list[Pred] | None, str]:
    """현재 구성과 지문이 같고 완전성 표시가 된 예측만 돌려준다. 아니면 (None, 사유)."""
    marker = load_marker(model, universe, horizon, template, checkpoint)
    if isinstance(marker, str):
        return None, marker
    return read_preds(Path(marker["_preds"])), "ok"


def load_marker(
    model: str, universe: str, horizon: int, template: dict[str, Any], checkpoint: str | None
) -> dict[str, Any] | str:
    """현재 구성의 완전한 예측 표시(.done.json) 내용, 아니면 사유 문자열."""
    if model == "mitra" and checkpoint is None:
        return f"{model}/{universe}/N{horizon}: Mitra 체크포인트 확인 불가 → 예측 대조 불가"
    try:
        tickers = universe_tickers(universe)
    except (SystemExit, FileNotFoundError) as e:
        return f"유니버스 {universe} 없음 ({e})"
    path = preds_path(model, universe, horizon,
                      experiment_fingerprint(model, universe, template, tickers, checkpoint))
    marker = marker_path(path)
    if not path.exists() or not marker.exists():
        return f"{model}/{universe}/N{horizon}: 현재 구성의 예측 없음 (`{path.name}`)"
    info: dict[str, Any] = json.loads(marker.read_text())
    if not info.get("complete"):
        return f"{model}/{universe}/N{horizon}: 예측 불완전 — predict 재실행 필요"
    return {**info, "_preds": str(path)}


# -- 평가 ------------------------------------------------------------------------------------


def auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """Mann-Whitney AUC (동점은 평균 순위)."""
    pos = sum(labels)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return math.nan
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    rank_pos = sum(r for r, y in zip(ranks, labels, strict=True) if y == 1)
    return (rank_pos - pos * (pos + 1) / 2) / (pos * neg)


def keep(p: Pred, delta: float) -> bool:
    return p.p >= p.base + delta


@dataclass(slots=True)
class Discrimination:
    n: int
    auc: float
    kept_mean: float
    removed_mean: float
    n_removed: int


def discrimination(preds: Sequence[Pred], delta: float = 0.0) -> Discrimination:
    rows = [p for p in preds if p.label is not None and p.net_ret is not None]
    kept = [p.net_ret for p in rows if keep(p, delta) and p.net_ret is not None]
    removed = [p.net_ret for p in rows if not keep(p, delta) and p.net_ret is not None]
    return Discrimination(
        len(rows),
        auc([p.p for p in rows], [p.label for p in rows if p.label is not None]),
        statistics.fmean(kept) if kept else math.nan,
        statistics.fmean(removed) if removed else math.nan,
        len(removed),
    )


# 실험 전용 전략: 운용 눌림목 + 최초 진입 게이트. 게이트 외 동작은 PullbackDaily 와 동일.
_GATE: dict[tuple[str, date], bool] = {}
_ENTRY_START: list[date] = [EVAL_START]
GATED_ID = "pullback_daily_mlgate_exp"


class GatedPullback(PullbackDaily):
    meta: ClassVar[StrategyMeta] = StrategyMeta(
        id=GATED_ID, name="눌림목 + ML 진입 게이트 (실험 전용)",
        supported_resolutions=[Resolution.D1],
    )
    filtered: ClassVar[list[tuple[str, date]]] = []

    def _entry(self, ctx: StrategyContext) -> list[Intent]:
        intents = super()._entry(ctx)
        if not any(i.kind is IntentKind.ENTER for i in intents):
            return intents
        day = ctx.snapshot.ts.date()
        if day < _ENTRY_START[0]:
            return [Intent(kind=IntentKind.HOLD, side=Side.BUY, reason="before_eval")]
        if _GATE.get((ctx.snapshot.ticker, day), True) is False:
            GatedPullback.filtered.append((ctx.snapshot.ticker, day))
            return [Intent(kind=IntentKind.HOLD, side=Side.BUY, reason="ml_gate")]
        return intents


if GATED_ID not in all_strategies():
    register_strategy(GATED_ID)(GatedPullback)


@dataclass(slots=True)
class RunStats:
    label: str
    trades: int
    filtered: int
    total_ret: float
    mdd: float
    roll: dict[int, tuple[float, float, float]]  # 개월 -> (플러스 비율, 중앙값, 최악)
    bear: dict[str, float]


def daily_equity(result: BacktestResult, start: date) -> list[tuple[date, float]]:
    """평가 시작 직전 자본을 원점으로, 날짜별 마지막 평가금."""
    by_day: dict[date, float] = {}
    for ts, eq in result.equity_curve:
        by_day[ts.date()] = float(eq)
    days = sorted(by_day)
    origin = float(result.starting_equity)
    for d in days:
        if d < start:
            origin = by_day[d]
    return [(start - timedelta(days=1), origin), *((d, by_day[d]) for d in days if d >= start)]


def max_drawdown(curve: Sequence[tuple[date, float]]) -> float:
    peak, mdd = -math.inf, 0.0
    for _, v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def rolling_windows(
    curve: Sequence[tuple[date, float]], months: int
) -> tuple[float, float, float] | None:
    """아무 달에 시작한 ``months`` 개월 구간 수익의 (플러스 비율, 중앙값, 최악)."""
    points = [curve[0][1]]
    for i, (d, v) in enumerate(curve[1:], 1):
        nxt = curve[i + 1][0] if i + 1 < len(curve) else None
        if nxt is None or (nxt.year, nxt.month) != (d.year, d.month):
            points.append(v)  # 월말(또는 마지막 봉)
    rets = [points[j + months] / points[j] - 1 for j in range(len(points) - months)]
    if not rets:
        return None
    return (sum(r > 0 for r in rets) / len(rets), statistics.median(rets), min(rets))


def window_return(curve: Sequence[tuple[date, float]], start: date, end: date) -> float:
    """구간 수익. 구간 안 관측이 없거나 데이터가 종료일 전에 끝나면 NaN (동률 통과 방지)."""
    before = [v for d, v in curve if d < start]
    inside = [v for d, v in curve if start <= d <= end]
    if not before or not inside or not curve or curve[-1][0] < end:
        return math.nan
    return inside[-1] / before[-1] - 1


def tape_for(tickers: Sequence[str], start: date) -> list[Bar]:
    tape: list[Bar] = []
    for t in tickers:
        tape += [b for b in load_series(t, BARS_DIR / f"{t}.csv").bars() if b.ts.date() >= start]
    tape.sort(key=lambda b: b.ts)  # 안정 정렬: 같은 날짜는 종목 순서 유지
    return tape


def run_arm(
    label: str,
    template: dict[str, Any],
    tape: list[Bar],
    gate: dict[tuple[str, date], bool],
    eval_start: date = EVAL_START,
) -> RunStats:
    _GATE.clear()
    _GATE.update(gate)
    _ENTRY_START[0] = eval_start
    GatedPullback.filtered.clear()
    tmpl = StrategyTemplate.model_validate({**template, "strategy_id": GATED_ID})
    result = Backtester(tmpl, STARTING_EQUITY, CostModel(), liquidate_open_at_end=True).run(tape)
    curve = daily_equity(result, eval_start)
    roll = {m: r for m in (6, 12) if (r := rolling_windows(curve, m)) is not None}
    return RunStats(
        label, len(result.trades), len(set(GatedPullback.filtered)),
        curve[-1][1] / curve[0][1] - 1, max_drawdown(curve), roll,
        {name: window_return(curve, s, e) for name, s, e in BEAR_WINDOWS},
    )


def g1_pass(arm: RunStats, base: RunStats) -> tuple[bool, list[str]]:
    """사전등록 G1 ①~⑥. 실패 사유 목록을 함께 돌려준다."""
    why: list[str] = []
    for m in (12, 6):
        a, b = arm.roll.get(m), base.roll.get(m)
        if a is None or b is None:
            why.append(f"{m}M 구간 없음")
            continue
        for k, name in enumerate(("플러스비율", "중앙값", "최악")):
            if a[k] < b[k] - 1e-12:
                why.append(f"{m}M {name} {a[k]:+.4f} < {b[k]:+.4f}")
    if arm.mdd > base.mdd + 1e-12:
        why.append(f"MDD {arm.mdd:.4f} > {base.mdd:.4f}")
    for name, v in arm.bear.items():
        ref = base.bear.get(name, math.nan)
        if not (math.isfinite(v) and math.isfinite(ref)):
            why.append(f"하락장 {name} 데이터 부족 → 비교 불가")
        elif v < ref - 1e-12:
            why.append(f"하락장 {name} {v:+.4f} < {ref:+.4f}")
    if arm.filtered < G1_MIN_FILTERED:
        why.append(f"걸러진 진입 {arm.filtered} < {G1_MIN_FILTERED}")
    a12, b12 = arm.roll.get(12), base.roll.get(12)
    strict = arm.mdd < base.mdd - 1e-12 or (
        a12 is not None and b12 is not None and (a12[1] > b12[1] + 1e-12 or a12[2] > b12[2] + 1e-12)
    )
    if not strict:
        why.append("엄격 개선 없음 (12M 중앙값·최악·MDD 모두 동률 이하)")
    return not why, why


def g2_pass(results: dict[tuple[int, float], bool]) -> tuple[bool, str]:
    n0, d0 = CENTER
    neighbors = [(5, d0), (20, d0), (n0, -0.05), (n0, 0.05)]
    center_ok = results.get(CENTER, False)
    nb_ok = all(results.get(k, False) for k in neighbors)
    count = sum(results.values())
    ok = center_ok and nb_ok and count >= 6
    return ok, f"중심 {'✓' if center_ok else '✗'} · 이웃4 {'✓' if nb_ok else '✗'} · 통과 {count}/9"


def gate_from(preds: Sequence[Pred], delta: float) -> dict[tuple[str, date], bool]:
    return {(p.ticker, p.day): keep(p, delta) for p in preds if p.ticker in EVAL_TICKERS}


def _fmt(v: float) -> str:
    return "n/a" if not math.isfinite(v) else f"{v:+.2%}"


def _stats_row(r: RunStats) -> str:
    r12, r6 = r.roll.get(12), r.roll.get(6)

    def roll(x: tuple[float, float, float] | None) -> str:
        return "n/a" if x is None else f"{x[0]:.0%} / {x[1]:+.1%} / {x[2]:+.1%}"

    bear = " · ".join(f"{k} {_fmt(v)}" for k, v in r.bear.items())
    return (f"| {r.label} | {r.trades} | {r.filtered} | {_fmt(r.total_ret)} | {r.mdd:.1%} | "
            f"{roll(r12)} | {roll(r6)} | {bear} |")


def g0_section(
    lines: list[str], template: dict[str, Any], checkpoint: str | None, missing: list[str]
) -> bool:
    """G0 채점. 전제 자료(완전한 예측)가 없으면 ``missing`` 에 적고 판정 불가로 둔다."""
    n = CENTER[0]
    ok = True
    for universe in ("A", "B"):
        mitra, why_m = load_complete_preds("mitra", universe, n, template, checkpoint)
        logit, why_l = load_complete_preds("logit", universe, n, template, None)
        if mitra is None:
            lines.append(f"- 유니버스 {universe}: {why_m} → **G0 판정 불가**")
            missing.append(why_m)
            ok = False
            continue
        if universe == "A" and logit is None:  # Mitra > 로지스틱 비교의 전제
            missing.append(why_l)
        if logit is not None and {(p.ticker, p.day) for p in logit} != {
            (p.ticker, p.day) for p in mitra
        }:
            lines.append(f"- 유니버스 {universe}: Mitra/로지스틱 표본 불일치 → **G0 판정 불가**")
            missing.append(f"유니버스 {universe} Mitra/로지스틱 표본 불일치")
            ok = False
            continue
        if universe == "B":
            lines.append(f"  - ({unavailable_note()})")
        if logit is None:
            lines.append(f"  - ({why_l})")
        pool = [p for p in mitra if p.label is not None]
        dm = discrimination(pool)
        dl = discrimination([p for p in logit if p.label is not None]) if logit is not None else None
        logit_desc = f"로지스틱 AUC {dl.auc:.4f}" if dl is not None else "로지스틱 예측 없음"
        lines.append(f"- 유니버스 {universe} (N={n}, 표본 {dm.n}): Mitra AUC **{dm.auc:.4f}**"
                     f" · {logit_desc}")
        checks = [("AUC ≥ 0.53", dm.auc >= G0_MIN_AUC)]
        if universe == "A":
            checks.append(("Mitra > 로지스틱", dl is not None and dm.auc > dl.auc))
        spread_ok = dm.removed_mean < dm.kept_mean
        lines.append(f"  - 전체: 통과 평균 {_fmt(dm.kept_mean)} vs 제외 평균 {_fmt(dm.removed_mean)}"
                     f" (제외 {dm.n_removed})")
        for name, s, e in BEAR_WINDOWS:
            w = discrimination([p for p in pool if s <= p.day <= e])
            if w.n_removed >= G0_MIN_REMOVED_PER_WINDOW:
                spread_ok = spread_ok and w.removed_mean < w.kept_mean
                lines.append(f"  - 하락장 {name}: 통과 {_fmt(w.kept_mean)} vs 제외 "
                             f"{_fmt(w.removed_mean)} (제외 {w.n_removed})")
            else:
                lines.append(f"  - 하락장 {name}: 제외 표본 {w.n_removed} < "
                             f"{G0_MIN_REMOVED_PER_WINDOW} → 판정 제외")
        checks.append(("제외 평균 < 통과 평균 (전체·하락장)", spread_ok))
        for name, passed in checks:
            lines.append(f"  - {'✓' if passed else '✗'} {name}")
            ok = ok and passed
    return ok


def final_verdict(missing: Sequence[str], g0: bool, g1_center: bool, g2: bool, g3: bool) -> str:
    """전제 자료가 하나라도 없으면 '판정 불가' — 미비를 가설 실패(기각)로 기록하지 않는다."""
    if missing:
        return "판정 불가"
    return "채택" if g0 and g1_center and g2 and g3 else "기각"


def _write_report(lines: list[str]) -> None:
    report = "\n".join(lines) + "\n"
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "report.md").write_text(report, encoding="utf-8")
    print(report)


def _verdict_lines(verdict: str, missing: Sequence[str], detail: str) -> list[str]:
    out = ["", "## 판정", "", f"**{verdict}** — {detail}"]
    if verdict == "판정 불가":
        out += ["", "미비 자료:", *(f"- {m}" for m in dict.fromkeys(missing)), "",
                "> 판정 불가 — BACKLOG.md·STRATEGIES.md 에 채택/기각으로 기록하지 말 것. "
                "미비 자료를 채운 뒤 evaluate 재실행."]
    else:
        out += ["", "> 판정은 BACKLOG.md·STRATEGIES.md 에 옮겨 적는다 (사전등록 H-M1 기준)."]
    return out


def cmd_evaluate(args: argparse.Namespace) -> None:
    template, source = load_template(_watchlist_arg(args.watchlist))
    lines = [
        f"# H-M1 실험 결과 ({date.today().isoformat()})", "",
        f"- 템플릿: {source} — `{json.dumps(template, ensure_ascii=False)}`",
        f"- 평가: {', '.join(EVAL_TICKERS)} · {EVAL_START} ~ 데이터 끝 · 시작자본 {STARTING_EQUITY:,}",
    ]
    missing: list[str] = []
    try:  # 평가 일봉·유니버스 A 선검증 — 없으면 백테스트 없이 판정 불가 보고서만
        universe_tickers("A")
    except (SystemExit, FileNotFoundError) as e:
        missing.append(f"유니버스 A/평가 일봉 미비 ({e})")
        _write_report(lines + _verdict_lines("판정 불가", missing, "평가 전제 자료 없음"))
        return

    checkpoint: str | None = None
    try:
        _ckpt_dir, checkpoint = resolve_checkpoint(args.hf_model)
        lines.append(f"- Mitra 체크포인트: `{args.hf_model}` → 내용 해시 `{checkpoint}`")
    except SystemExit as e:
        missing.append(str(e))
        lines.append(f"- Mitra 체크포인트 확인 불가: {e}")

    lines += ["", "## G0 판별력", ""]
    g0 = g0_section(lines, template, checkpoint, missing)

    tape = tape_for(EVAL_TICKERS, EVAL_START - timedelta(days=450))  # 지표 창(300봉) 포화
    base = run_arm("현행", template, tape, {})
    for m in (12, 6):
        if m not in base.roll:
            missing.append(f"롤링 {m}개월 구간을 만들 데이터 부족")
    for name, v in base.bear.items():
        if not math.isfinite(v):
            missing.append(f"하락장 {name} 구간 데이터 부족")
    arms: dict[tuple[int, float], RunStats] = {}
    passes: dict[tuple[int, float], bool] = {}
    reasons: dict[tuple[int, float], list[str]] = {}
    for n in HORIZONS:
        preds, why = load_complete_preds("mitra", "A", n, template, checkpoint)
        if preds is None:
            missing.append(why)
        for d in DELTAS:
            if preds is None:
                passes[(n, d)] = False
                reasons[(n, d)] = [why]
                continue
            arm = run_arm(f"N={n} δ={d:+.2f}", template, tape, gate_from(preds, d))
            arms[(n, d)] = arm
            passes[(n, d)], reasons[(n, d)] = g1_pass(arm, base)

    lines += [
        "", "## G1 전략 성적 (유니버스 A 예측)", "",
        "| 설정 | 거래 | 걸러짐 | 총수익 | MDD | 12M 플러스/중앙/최악 | 6M 플러스/중앙/최악 | 하락장 |",
        "|---|---|---|---|---|---|---|---|",
        _stats_row(base),
        *(_stats_row(a) for a in arms.values()),
        "",
    ]
    for k, ok in passes.items():
        lines.append(f"- N={k[0]} δ={k[1]:+.2f}: {'✓ 통과' if ok else '✗ ' + '; '.join(reasons[k])}")
    g2, g2_desc = g2_pass(passes)
    lines += ["", "## G2 견고 클러스터", "", f"- {g2_desc} → {'✓' if g2 else '✗'}"]

    lat_path = ROOT / "latency.json"
    lat = json.loads(lat_path.read_text()) if lat_path.exists() else None
    center_run = load_marker("mitra", "A", CENTER[0], template, checkpoint)
    run = center_run.get("run") if isinstance(center_run, dict) else None
    lat_ok = lat is not None and latency_matches(lat, checkpoint, run)
    if lat is None:
        missing.append("latency 측정 없음 (`latency` 실행)")
    elif not lat_ok:
        missing.append("latency 측정이 현재 구성·예측 실행 경로와 다름 (같은 장치·경로로 재측정)")
        lat = {**lat, "불일치": f"현재 구성(체크포인트 {checkpoint}, 지지 {MAX_SUPPORT}, "
                              f"피처 {len(FEATURES)}, 예측 실행 {run})과 다름 → 같은 장치·경로로 "
                              "latency 재측정 필요"}
    g3 = lat is not None and lat_ok and float(lat["seconds"]) <= G3_MAX_LATENCY_S
    lines += ["", "## G3 지연", "",
              f"- {lat if lat else '측정 없음 (`latency` 먼저 실행)'} → {'✓' if g3 else '✗'}"]

    g1c = passes.get(CENTER, False)
    verdict = final_verdict(missing, g0, g1c, g2, g3)
    detail = (f"G0 {'✓' if g0 else '✗'} · G1(중심) {'✓' if g1c else '✗'} · "
              f"G2 {'✓' if g2 else '✗'} · G3 {'✓' if g3 else '✗'}")
    _write_report(lines + _verdict_lines(
        "채택 (배선 단계로)" if verdict == "채택" else verdict, missing, detail
    ))


def latency_matches(
    lat: dict[str, Any], checkpoint: str | None, run: dict[str, Any] | None
) -> bool:
    """G3 는 현재 평가 구성(체크포인트·지지 크기·피처 수)과, 실제 예측에 쓴 실행 경로
    (``run`` = 예측 완료 표시의 장치·빠른/공개 경로)로 잰 측정만 인정한다."""
    return (
        run is not None and bool(run)
        and checkpoint is not None and lat.get("checkpoint") == checkpoint
        and lat.get("support") == MAX_SUPPORT
        and lat.get("features") == len(FEATURES)
        and lat.get("device") == run.get("device") and lat.get("fast") == run.get("fast")
    )


def cmd_latency(args: argparse.Namespace) -> None:
    """지지 5,000행, 32피처, 질의 1행 — 모델 적재 포함 1회 예측 시간(3회 중 최솟값)."""
    rng = np.random.default_rng(0)
    xs = rng.normal(size=(MAX_SUPPORT, len(FEATURES)))
    ys = (xs[:, 0] + rng.normal(size=MAX_SUPPORT) > 0).astype(np.int64)
    xq = rng.normal(size=(1, len(FEATURES)))
    ckpt_dir, checkpoint = resolve_checkpoint(args.hf_model)
    predictor = MitraPredictor(ckpt_dir, args.device, fast=not args.slow)
    times: list[float] = []
    for _ in range(3):
        t0 = time.perf_counter()
        predictor.predict(xs, ys, xq)
        times.append(time.perf_counter() - t0)
    info = {"seconds": round(min(times), 2), "runs": [round(t, 2) for t in times],
            "hf_model": args.hf_model, "checkpoint": checkpoint,
            "device": predictor.device, "fast": predictor.fast,
            "support": MAX_SUPPORT,
            "features": len(FEATURES)}
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "latency.json").write_text(json.dumps(info))
    print(info)


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="FDR 일봉 다운로드·캐시")
    f.add_argument("--delisted", action="store_true", help="유니버스 B(상폐 포함)")
    f.add_argument("--refresh", action="store_true", help="캐시된 일봉도 다시 받기")
    f.add_argument("--refresh-universe", action="store_true", help="시총 상위 목록 다시 뽑기")
    for name in ("predict", "evaluate", "latency"):
        p = sub.add_parser(name)
        p.add_argument("--watchlist", default="watchlist.json",
                       help="평가 템플릿 원천. none = 운용 기본값 명시 선택")
        p.add_argument("--hf-model", default=MITRA_HF_MODEL)
        p.add_argument("--device", default="auto", help="auto | cpu | mps | cuda")
        if name == "predict":
            p.add_argument("--model", choices=("mitra", "logit"), required=True)
            p.add_argument("--universe", choices=("A", "B"), default="A")
            p.add_argument("--horizons", default=",".join(map(str, HORIZONS)))
            p.add_argument("--jobs", type=int, default=4)
        if name in ("predict", "latency"):
            p.add_argument("--slow", action="store_true",
                           help="Mitra 공개 API 경로 (빠른 경로가 AutoGluon 버전 차이로 깨질 때)")
    args = ap.parse_args(argv)
    {"fetch": cmd_fetch, "predict": cmd_predict, "evaluate": cmd_evaluate,
     "latency": cmd_latency}[args.cmd](args)


if __name__ == "__main__":
    main(sys.argv[1:])
