"""scripts/mitra_filter_experiment.py (H-M1) — 선견 차단·게이트 동등성·채점 로직 검증.

실험 결과의 신뢰는 이 불변식에 달려 있다:
- 지지 집합에는 라벨 확정일이 블록 시작일보다 앞선 표본만 들어간다.
- 게이트가 비어 있으면 실험 전략은 운용 눌림목과 거래가 완전히 같다.
- 표본(셋업 봉)은 백테스트 진입일을 빠짐없이 덮는다 (게이트 키가 진입과 맞물린다).
"""

from __future__ import annotations

import importlib.util
import math
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from short_trading_bot.backtest.costs import CostModel
from short_trading_bot.backtest.harness import Backtester
from short_trading_bot.domain.enums import Market
from short_trading_bot.strategy.templates import StrategyTemplate

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mitra_filter_experiment.py"


def _load() -> ModuleType:
    name = "mitra_filter_experiment"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mx = _load()

# 가드 없는 기본 눌림목 — 합성 시계열에서 진입이 충분히 나오게.
TEMPLATE: dict[str, Any] = {
    "strategy_id": "pullback_daily_v1", "market": "KRX", "resolution": "1D",
    "risk_per_trade": 0.02, "strategy_params": {"rsi_min": 35, "rsi_max": 65,
                                                "touch_band_pct": 0.02},
}


def _series(ticker: str, n: int, seed: int, drift: float = 0.0015) -> Any:
    rng = np.random.default_rng(seed)
    days: list[date] = []
    d = date(2012, 1, 2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    rets = drift + 0.02 * np.sin(np.arange(n) / 9.0) * 0.5 + rng.normal(0, 0.015, n)
    close = np.round(10000 * np.exp(np.cumsum(rets)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n))
    volume = np.round(rng.uniform(5e5, 2e6, n))
    return mx.Series(ticker, days, open_, np.round(high), np.round(low), close, volume)


# -- 비용·라벨 --------------------------------------------------------------------------


def test_net_return_matches_cost_model() -> None:
    cost = CostModel()
    r = mx.net_return(100.0, 100.0, cost)
    # 슬리피지 5bp 양방향 + 수수료 1.77bp 양방향 + 거래세 20bp ≈ -0.335%
    assert r == pytest.approx(-0.003352, abs=2e-5)
    assert mx.net_return(100.0, 101.0, cost) > 0 > mx.net_return(100.0, 100.3, cost)
    # Backtester 와 같은 방식: 매도 금액에서 수수료·세금
    buy = Decimal(100) * Decimal("1.0005")
    sell = Decimal(101) * Decimal("0.9995")
    net = (sell - cost.fee(sell) - cost.sell_tax(sell, Market.KRX)) / (buy + cost.fee(buy)) - 1
    assert mx.net_return(100.0, 101.0, cost) == pytest.approx(float(net), rel=1e-9)


# -- 워크포워드 선견 차단 ------------------------------------------------------------------


class _Spy:
    name = "spy"

    def __init__(self) -> None:
        self.calls: list[tuple[int, NDArray[np.int64], NDArray[np.float64]]] = []

    def predict(
        self, xs: NDArray[np.float64], ys: NDArray[np.int64], xq: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        self.calls.append((len(xs), ys.copy(), xs.copy()))
        return np.full(len(xq), 0.5)


def _synthetic_samples(n_days: int = 400) -> list[Any]:
    """x[0] = 표본 일련번호(날짜 순서), 라벨 확정일 = day + 10일."""
    out = []
    start = date(2015, 1, 1)
    for i in range(n_days):
        d = start + timedelta(days=i)
        out.append(mx.Sample("T", d, (float(i), 0.0), True,
                             {10: (d + timedelta(days=10), 0.01 if i % 2 else -0.01)}))
    return out


def test_walk_forward_support_labels_strictly_before_block() -> None:
    samples = _synthetic_samples()
    blocks = [date(2015, 1, 1) + timedelta(days=k) for k in range(0, 400, 7)]
    spy = _Spy()
    preds = mx.walk_forward(samples, samples, 10, spy, blocks, min_support=20, min_class=5,
                            max_support=10_000)
    assert preds and spy.calls
    by_block = {p.block for p in preds}
    for p in preds:
        assert p.block <= p.day  # 질의는 자기 블록 안
    # 각 호출의 지지 집합 최대 일련번호 → 라벨 확정일(= 시작일 + idx + 10) < 블록 시작일
    for (n_sup, _ys, xs), b in zip(spy.calls, sorted(by_block), strict=True):
        last_idx = int(xs[:, 0].max())
        label_day = date(2015, 1, 1) + timedelta(days=last_idx + 10)
        assert label_day < b
        # 그리고 가능한 최신까지 쓴다 (라벨 확정일 = b-1 인 표본까지)
        assert label_day == b - timedelta(days=1)
        assert n_sup == last_idx + 1


def test_walk_forward_caps_to_most_recent_support() -> None:
    samples = _synthetic_samples()
    blocks = [date(2015, 12, 1)]
    spy = _Spy()
    mx.walk_forward(samples, samples, 10, spy, blocks, min_support=20, min_class=5,
                    max_support=50)
    n_sup, _ys, xs = spy.calls[0]
    assert n_sup == 50
    newest = int(xs[:, 0].max())
    assert int(xs[:, 0].min()) == newest - 49  # 가장 최근 50개


def test_walk_forward_skips_thin_support_and_resumes() -> None:
    samples = _synthetic_samples()
    blocks = [date(2015, 1, 1), date(2015, 1, 20), date(2015, 6, 1)]
    spy = _Spy()
    preds = mx.walk_forward(samples, samples, 10, spy, blocks, min_support=100, min_class=5)
    assert {p.block for p in preds} == {date(2015, 6, 1)}  # 앞 두 블록은 지지 부족 → 예측 없음
    spy2 = _Spy()
    again = mx.walk_forward(samples, samples, 10, spy2, blocks, min_support=100, min_class=5,
                            done={date(2015, 6, 1)})
    assert again == [] and spy2.calls == []


def test_split_pool_queries() -> None:
    a = mx.Sample("A", date(2016, 3, 2), (0.0,), True)
    b = mx.Sample("B", date(2016, 3, 2), (0.0,), False)  # 비유동 → 풀·질의 제외
    c = mx.Sample("005930", date(2016, 3, 2), (0.0,), False)  # 평가 종목은 비유동이어도 질의
    d = mx.Sample("A", date(2015, 3, 2), (0.0,), True)  # 평가 전 → 풀만
    pool, queries = mx.split_pool_queries([a, b, c, d], ("005930",), date(2016, 1, 1))
    assert pool == [a, d]
    assert queries == [a, c]


def test_logit_predictor_learns_signal() -> None:
    rng = np.random.default_rng(1)
    xs = rng.normal(size=(2000, 5))
    ys = (xs[:, 0] - xs[:, 1] + rng.normal(0, 0.5, 2000) > 0).astype(np.int64)
    xq = rng.normal(size=(1000, 5))
    yq = (xq[:, 0] - xq[:, 1] > 0).astype(int)
    p = mx.LogitPredictor().predict(xs, ys, xq)
    assert mx.auc(list(p), list(yq)) > 0.9


# -- 채점 -------------------------------------------------------------------------------


def test_auc_known_values() -> None:
    assert mx.auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert mx.auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
    assert mx.auc([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1]) == 0.5
    assert mx.auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75
    assert math.isnan(mx.auc([0.1, 0.2], [1, 1]))


def test_rolling_windows_and_drawdown() -> None:
    # 원점 100 → 월말 110, 99, 120 (1·2·3월)
    curve = [(date(2015, 12, 31), 100.0), (date(2016, 1, 15), 105.0), (date(2016, 1, 29), 110.0),
             (date(2016, 2, 29), 99.0), (date(2016, 3, 31), 120.0)]
    plus, med, worst = mx.rolling_windows(curve, 1)
    assert worst == pytest.approx(-0.1)  # 110 → 99
    assert plus == pytest.approx(2 / 3)
    assert med == pytest.approx(0.1)
    two = mx.rolling_windows(curve, 2)
    assert two[2] == pytest.approx(-0.01) and two[0] == pytest.approx(0.5)  # 100→99, 110→120
    assert mx.rolling_windows(curve, 4) is None
    assert mx.max_drawdown(curve) == pytest.approx(0.1)
    assert mx.window_return(curve, date(2016, 2, 1), date(2016, 2, 29)) == pytest.approx(-0.1)


def _stats(label: str, roll12: tuple[float, float, float], mdd: float, filtered: int = 10,
           bear: float = 0.0) -> Any:
    return mx.RunStats(label, 20, filtered, 0.5, mdd, {12: roll12, 6: (0.6, 0.02, -0.1)},
                       {"2018": bear, "2022": bear, "2026-07": bear})


def test_g1_pass_rules() -> None:
    base = _stats("현행", (0.7, 0.10, -0.2), 0.15)
    ok, why = mx.g1_pass(_stats("a", (0.7, 0.12, -0.2), 0.15), base)
    assert ok, why
    # 동률뿐 → 엄격 개선 없음으로 기각
    ok, why = mx.g1_pass(_stats("b", (0.7, 0.10, -0.2), 0.15), base)
    assert not ok and any("엄격 개선" in w for w in why)
    # MDD 악화
    assert not mx.g1_pass(_stats("c", (0.7, 0.12, -0.2), 0.16), base)[0]
    # 하락장 악화
    assert not mx.g1_pass(_stats("d", (0.7, 0.12, -0.2), 0.15, bear=-0.01), base)[0]
    # 걸러진 진입 부족
    assert not mx.g1_pass(_stats("e", (0.7, 0.12, -0.2), 0.15, filtered=4), base)[0]


def test_g2_cluster_rule() -> None:
    all_ok = {(n, d): True for n in mx.HORIZONS for d in mx.DELTAS}
    assert mx.g2_pass(all_ok)[0]
    corner_fail = dict(all_ok)
    corner_fail[(5, -0.05)] = corner_fail[(20, 0.05)] = corner_fail[(5, 0.05)] = False
    assert mx.g2_pass(corner_fail)[0]  # 6/9 + 중심·이웃 통과
    neighbor_fail = dict(all_ok)
    neighbor_fail[(10, 0.05)] = False
    assert not mx.g2_pass(neighbor_fail)[0]  # 단일 최고점 금지: 이웃 하나라도 실패하면 기각


# -- 백테스트 동등성 ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def synth() -> tuple[list[Any], list[Any]]:
    series = [_series("005930", 900, 3), _series("000660", 900, 4)]
    tape = sorted((b for s in series for b in s.bars()), key=lambda b: b.ts)
    return series, tape


def test_empty_gate_equals_live_pullback(synth: tuple[list[Any], list[Any]]) -> None:
    _series_list, tape = synth
    live = Backtester(StrategyTemplate.model_validate(TEMPLATE), mx.STARTING_EQUITY, CostModel(),
                      liquidate_open_at_end=True).run(tape)
    assert len(live.trades) >= 3  # 합성 시계열이 실제로 진입을 만든다
    stats = mx.run_arm("현행", TEMPLATE, tape, {}, eval_start=date(2000, 1, 1))
    assert stats.trades == len(live.trades)
    assert stats.filtered == 0
    assert stats.total_ret == pytest.approx(float(live.final_equity / live.starting_equity) - 1)


def test_samples_cover_every_entry_and_gate_blocks(synth: tuple[list[Any], list[Any]]) -> None:
    series, tape = synth
    ks = _series(mx.KS11, 900, 9)
    mkt = mx.market_features(ks)
    live = Backtester(StrategyTemplate.model_validate(TEMPLATE), mx.STARTING_EQUITY, CostModel(),
                      liquidate_open_at_end=True).run(tape)
    setup_days = {
        (smp.ticker, smp.day)
        for s in series for smp in mx.build_samples(s, mkt, TEMPLATE, CostModel())
    }
    first_full = {s.ticker: s.days[260] for s in series}  # 252일 고점 피처가 생긴 뒤
    entries = {(t.ticker, t.opened_at.date()) for t in live.trades
               if t.opened_at.date() >= first_full[t.ticker]}
    assert entries and entries <= setup_days
    # 모든 셋업을 막으면 (평가 시작 이후) 신규 진입 0
    gate = dict.fromkeys(setup_days, False)
    start = max(first_full.values())
    blocked = mx.run_arm("차단", TEMPLATE, tape, gate, eval_start=start)
    assert blocked.trades == 0 and blocked.filtered > 0


def test_build_samples_features_are_point_in_time() -> None:
    """t 이후 봉을 바꿔도 t 의 피처는 그대로여야 한다 (라벨만 바뀜)."""
    s = _series("005930", 700, 5)
    ks = _series(mx.KS11, 700, 9)
    base = {(x.day): x for x in mx.build_samples(s, mx.market_features(ks), TEMPLATE, CostModel())}
    cut = 500
    s2 = mx.Series(s.ticker, s.days, s.open.copy(), s.high.copy(), s.low.copy(), s.close.copy(),
                   s.volume.copy())
    s2.close[cut:] *= 1.3
    s2.high[cut:] *= 1.3
    s2.low[cut:] *= 1.3
    s2.open[cut:] *= 1.3
    changed = {x.day: x for x in mx.build_samples(s2, mx.market_features(ks), TEMPLATE, CostModel())}
    early = [d for d in base if d < s.days[cut]]
    assert early
    for d in early:
        assert changed[d].x == base[d].x
    assert len(mx.FEATURES) == 32 == len(next(iter(base.values())).x)


# -- Mitra 어댑터 (autogluon 설치 시에만 — 본체 의존성 아님) --------------------------------


def test_mitra_adapter_fast_and_public_paths(tmp_path: Path) -> None:
    pytest.importorskip("autogluon.tabular.models.mitra.sklearn_interface")
    from autogluon.tabular.models.mitra._internal.models.tab2d import Tab2D

    # 가중치는 무작위 소형 모델 — 배관(형상·확률 열·지지 집합 전달)만 검증한다.
    Tab2D(dim=64, dim_output=10, n_layers=2, n_heads=4, task="CLASSIFICATION",
          use_pretrained_weights=False, path_to_weights="", device="cpu").save_pretrained(
        str(tmp_path / "tiny"))
    rng = np.random.default_rng(0)
    xs = rng.normal(size=(200, 8))
    ys = (xs[:, 0] > 0).astype(np.int64)
    xq = rng.normal(size=(7, 8))
    for fast in (True, False):
        p = mx.MitraPredictor(str(tmp_path / "tiny"), "cpu", fast=fast).predict(xs, ys, xq)
        assert p.shape == (7,)
        assert np.all((p >= 0) & (p <= 1))


# -- Codex 리뷰 반영 회귀 테스트 ----------------------------------------------------------


def test_window_return_requires_observations_and_coverage() -> None:
    curve = [(date(2017, 12, 29), 100.0), (date(2018, 6, 1), 90.0), (date(2019, 1, 2), 95.0)]
    assert mx.window_return(curve, date(2018, 1, 1), date(2018, 12, 31)) == pytest.approx(-0.1)
    # 구간 안 관측 없음 → 0% 동률이 아니라 NaN
    gap = [(date(2017, 12, 29), 100.0), (date(2019, 1, 2), 95.0)]
    assert math.isnan(mx.window_return(gap, date(2018, 1, 1), date(2018, 12, 31)))
    # 데이터가 구간 종료 전에 끝남 → NaN
    short = [(date(2026, 6, 30), 100.0), (date(2026, 7, 15), 97.0)]
    assert math.isnan(mx.window_return(short, date(2026, 7, 1), date(2026, 8, 31)))


def test_g1_fails_when_bear_window_not_comparable() -> None:
    base = _stats("현행", (0.7, 0.10, -0.2), 0.15, bear=math.nan)
    ok, why = mx.g1_pass(_stats("a", (0.7, 0.12, -0.2), 0.15, bear=math.nan), base)
    assert not ok and any("데이터 부족" in w for w in why)


def _pred(ticker: str, day: date, block: date) -> Any:
    return mx.Pred(ticker, day, block, 0.5, 0.5, 300, 1, 0.01)


def test_completeness_rejects_partial_duplicate_and_extra_blocks() -> None:
    b1, b2 = date(2016, 1, 4), date(2016, 1, 11)
    expected = {b1: {("A", date(2016, 1, 4)), ("B", date(2016, 1, 5))},
                b2: {("A", date(2016, 1, 12))}}
    full = [_pred("A", date(2016, 1, 4), b1), _pred("B", date(2016, 1, 5), b1),
            _pred("A", date(2016, 1, 12), b2)]
    assert mx.is_complete(full, expected)
    partial = full[:1] + full[2:]  # b1 반쯤 기록 (중단)
    assert mx.complete_blocks(partial, expected) == {b2}
    assert not mx.is_complete(partial, expected)
    dup = [*full, _pred("A", date(2016, 1, 12), b2)]
    assert mx.complete_blocks(dup, expected) == {b1}
    extra = [*full, _pred("C", date(2016, 1, 20), date(2016, 1, 18))]
    assert not mx.is_complete(extra, expected)


def test_plan_blocks_matches_walk_forward_output() -> None:
    samples = _synthetic_samples()
    blocks = [date(2015, 1, 1) + timedelta(days=k) for k in range(0, 400, 7)]
    _support, plans = mx.plan_blocks(samples, samples, 10, blocks, min_support=20, min_class=5)
    preds = mx.walk_forward(samples, samples, 10, _Spy(), blocks, min_support=20, min_class=5)
    assert mx.is_complete(preds, mx.expected_keys(plans))


def test_fingerprint_tracks_config_and_bars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mx, "BARS_DIR", tmp_path)
    (tmp_path / "KS11.csv").write_text("date,open,high,low,close,volume\n")
    (tmp_path / "005930.csv").write_text("date,open,high,low,close,volume\n")
    fp = mx.experiment_fingerprint("mitra", "A", TEMPLATE, ["005930"], "m2")
    assert fp == mx.experiment_fingerprint("mitra", "A", TEMPLATE, ["005930"], "m2")
    changed = {**TEMPLATE, "risk_per_trade": 0.01}
    assert fp != mx.experiment_fingerprint("mitra", "A", changed, ["005930"], "m2")
    assert fp != mx.experiment_fingerprint("mitra", "A", TEMPLATE, ["005930"], "other")
    assert fp != mx.experiment_fingerprint("mitra", "A", TEMPLATE, ["005930", "000660"], "m2")
    (tmp_path / "005930.csv").write_text("date,open,high,low,close,volume\n2016-01-04,1,1,1,1,1\n")
    assert fp != mx.experiment_fingerprint("mitra", "A", TEMPLATE, ["005930"], "m2")
    # 로지스틱은 체크포인트와 무관
    assert mx.experiment_fingerprint("logit", "A", TEMPLATE, ["005930"], "x") == \
        mx.experiment_fingerprint("logit", "A", TEMPLATE, ["005930"], "y")


# -- Codex 2차 리뷰 반영 ----------------------------------------------------------------


def _universe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bars: list[str],
              u: dict[str, Any]) -> None:
    import json

    monkeypatch.setattr(mx, "BARS_DIR", tmp_path / "bars")
    monkeypatch.setattr(mx, "UNIVERSE_FILE", tmp_path / "universe.json")
    (tmp_path / "bars").mkdir()
    for t in bars:
        (tmp_path / "bars" / f"{t}.csv").write_text("date,open,high,low,close,volume\n")
    (tmp_path / "universe.json").write_text(json.dumps(u))


def test_universe_rejects_missing_bars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    u = {"eval": ["005930"], "top": ["000660", "005380"], "delisted": ["111110", "222220"],
         "unavailable": ["222220"]}
    _universe(tmp_path, monkeypatch, ["KS11", "005930", "000660", "111110"], u)
    with pytest.raises(SystemExit, match="005380"):  # 필수 종목 누락 → 조용히 빼지 않고 중단
        mx.universe_tickers("A")


def test_universe_b_excludes_only_recorded_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    u = {"eval": ["005930"], "top": ["000660"], "delisted": ["111110", "222220", "333330"],
         "unavailable": ["222220"]}
    _universe(tmp_path, monkeypatch, ["KS11", "005930", "000660", "111110", "333330"], u)
    assert mx.universe_tickers("B") == ["005930", "000660", "111110", "333330"]
    (tmp_path / "bars" / "333330.csv").unlink()  # 기록되지 않은 누락 → 거부
    with pytest.raises(SystemExit, match="333330"):
        mx.universe_tickers("B")


def test_sample_cache_invalidated_by_ks11_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.setattr(mx, "BARS_DIR", tmp_path)
    (tmp_path / "KS11.csv").write_text("a\n")
    (tmp_path / "005930.csv").write_text("b\n")
    cache = tmp_path / "005930.json"
    cache.write_text(json.dumps({"bars_sha": mx.file_sha(tmp_path / "005930.csv"),
                                 "ks_sha": mx.file_sha(tmp_path / "KS11.csv"), "rows": []}))
    assert mx._cache_fresh(cache, tmp_path / "005930.csv")
    (tmp_path / "KS11.csv").write_text("changed\n")
    assert not mx._cache_fresh(cache, tmp_path / "005930.csv")


def test_latency_must_match_evaluation_config() -> None:
    lat = {"seconds": 10.0, "hf_model": "m2", "support": mx.MAX_SUPPORT,
           "features": len(mx.FEATURES)}
    assert mx.latency_matches(lat, "m2")
    assert not mx.latency_matches(lat, "other")
    assert not mx.latency_matches({**lat, "support": 1000}, "m2")
    assert not mx.latency_matches({k: v for k, v in lat.items() if k != "hf_model"}, "m2")
