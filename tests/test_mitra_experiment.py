"""scripts/mitra_filter_experiment.py (H-M1) — 선견 차단·게이트 동등성·채점 로직 검증.

실험 결과의 신뢰는 이 불변식에 달려 있다:
- 지지 집합에는 라벨 확정일이 블록 시작일보다 앞선 표본만 들어간다.
- 게이트가 비어 있으면 실험 전략은 운용 눌림목과 거래가 완전히 같다.
- 표본(셋업 봉)은 백테스트 진입일을 빠짐없이 덮는다 (게이트 키가 진입과 맞물린다).
"""

from __future__ import annotations

import errno
import importlib.util
import math
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

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
        predictor = mx.MitraPredictor(str(tmp_path / "tiny"), "cpu", fast=fast)
        assert (predictor.device, predictor.fast) == ("cpu", fast)  # G3 대조용 실행 경로
        p = predictor.predict(xs, ys, xq)
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
        (tmp_path / "bars" / f"{t}.csv").write_text(
            "date,open,high,low,close,volume\n2010-01-04,1,1,1,1,1\n2016-01-04,1,1,1,1,1\n"
        )
    (tmp_path / "universe.json").write_text(
        json.dumps({"fetch_complete": True, "schema": mx.UNIVERSE_SCHEMA,
                    "top_listed": {c: "2005-01-03" for c in TOP}, **u})
    )


TOP = [f"9{i:04d}0" for i in range(1, mx.UNIVERSE_TOP + 1)]  # 평가 종목과 겹치지 않게
EVAL = list(mx.EVAL_TICKERS)


def test_universe_rejects_missing_bars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    u = {"eval": EVAL, "top": TOP, "delisted": ["111110", "222220"], "unavailable": ["222220"]}
    _universe(tmp_path, monkeypatch, ["KS11", *EVAL, *TOP[:-1], "111110"], u)
    with pytest.raises(SystemExit, match=TOP[-1]):  # 필수 종목 누락 → 조용히 빼지 않고 중단
        mx.universe_tickers("A")


def test_universe_rejects_incomplete_fetch_or_wrong_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    u = {"eval": EVAL, "top": TOP}
    _universe(tmp_path, monkeypatch, ["KS11", *EVAL, *TOP], u)
    assert len(mx.universe_tickers("A")) == len(EVAL) + mx.UNIVERSE_TOP
    uf = tmp_path / "universe.json"
    uf.write_text(json.dumps({**u, "fetch_complete": False}))  # 새로고침 중단
    with pytest.raises(SystemExit, match="완료되지 않음"):
        mx.universe_tickers("A")
    uf.write_text(json.dumps({**u, "fetch_complete": True, "required_failed": [EVAL[0]]}))
    with pytest.raises(SystemExit, match="조회 실패"):
        mx.universe_tickers("A")
    uf.write_text(json.dumps({"eval": EVAL, "top": TOP[:50], "fetch_complete": True,
                              "schema": mx.UNIVERSE_SCHEMA}))
    with pytest.raises(SystemExit, match="사전등록"):  # 상위 100 이 아닌 유니버스
        mx.universe_tickers("A")


def test_universe_b_excludes_only_recorded_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dl = [f"8{i:04d}0" for i in range(mx.MIN_DELISTED + 2)]
    u = {"eval": EVAL, "top": TOP, "delisted": dl, "unavailable": [dl[1]]}
    usable = [c for c in dl if c != dl[1]]
    _universe(tmp_path, monkeypatch, ["KS11", *EVAL, *TOP, *usable], u)
    assert mx.universe_tickers("B") == [*EVAL, *TOP, *usable]
    (tmp_path / "bars" / f"{dl[2]}.csv").unlink()  # 기록되지 않은 누락 → 거부
    with pytest.raises(SystemExit, match=dl[2]):
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
    run = {"device": "cpu", "fast": True}
    lat = {"seconds": 10.0, "checkpoint": "m2", "support": mx.MAX_SUPPORT,
           "features": len(mx.FEATURES), "device": "cpu", "fast": True,
           "code": mx.impl_key()}
    assert mx.latency_matches(lat, "m2", run)
    assert not mx.latency_matches(lat, "other", run)  # 가중치 내용이 다름
    assert not mx.latency_matches(lat, None, run)  # 체크포인트 확인 불가
    assert not mx.latency_matches({**lat, "support": 1000}, "m2", run)
    assert not mx.latency_matches({k: v for k, v in lat.items() if k != "checkpoint"}, "m2", run)
    # 실제 예측과 다른 장치·경로로 잰 측정은 불인정 (MPS 로 재고 CPU 로 예측 등)
    assert not mx.latency_matches({**lat, "device": "mps"}, "m2", run)
    assert not mx.latency_matches({**lat, "fast": False}, "m2", run)
    assert not mx.latency_matches(lat, "m2", None)  # 완전한 Mitra 예측 없음
    assert not mx.latency_matches({**lat, "code": "old"}, "m2", run)  # 실험 구현이 바뀜


def _watchlist(tmp_path: Path, entries: dict[str, Any]) -> Path:
    import json

    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps({"watchlist": entries}))
    return path


def test_load_template_requires_all_eval_tickers(tmp_path: Path) -> None:
    cfg = {**TEMPLATE}
    full = {f"{t}@1D": cfg for t in mx.EVAL_TICKERS}
    tmpl, source = mx.load_template(_watchlist(tmp_path, full))
    assert tmpl == cfg and source.endswith("watchlist.json")
    # 005380 누락 (watchlist.example.json 과 같은 형태) → 조용히 덮어쓰지 않고 중단
    partial = {k: v for k, v in full.items() if not k.startswith("005380")}
    with pytest.raises(SystemExit, match="005380"):
        mx.load_template(_watchlist(tmp_path, partial))
    # 한 종목이 다른 전략이면 역시 누락
    other = {**full, "005380@1D": {**cfg, "strategy_id": "trend_long_v1"}}
    with pytest.raises(SystemExit, match="005380"):
        mx.load_template(_watchlist(tmp_path, other))
    # 파라미터가 다르면 중단
    diff = {**full, "005380@1D": {**cfg, "risk_per_trade": 0.01}}
    with pytest.raises(SystemExit, match="서로 다름"):
        mx.load_template(_watchlist(tmp_path, diff))
    # 기본값은 `--watchlist none` 명시 선택일 때만 — 파일이 없으면 중단
    assert mx.load_template(None)[0] == mx.DEFAULT_TEMPLATE
    with pytest.raises(SystemExit, match="--watchlist none"):
        mx.load_template(tmp_path / "absent.json")


def test_claim_run_pins_execution_path(tmp_path: Path) -> None:
    path = tmp_path / "preds" / "mitra_A_N10_x.csv"
    cpu_fast = {"device": "cpu", "fast": True}
    mx.claim_run(path, cpu_fast)
    mx.claim_run(path, cpu_fast)  # 같은 경로로 재개는 허용
    with pytest.raises(SystemExit, match="실행 경로"):
        mx.claim_run(path, {"device": "mps", "fast": True})
    with pytest.raises(SystemExit, match="실행 경로"):
        mx.claim_run(path, {"device": "cpu", "fast": False})
    # 실행 경로 기록 없이 남은 예측 파일은 출처 불명 → 거부
    orphan = tmp_path / "preds" / "mitra_A_N5_x.csv"
    orphan.write_text("ticker\n")
    with pytest.raises(SystemExit, match="기록 없는"):
        mx.claim_run(orphan, cpu_fast)


# -- Codex 6차 리뷰 반영 ----------------------------------------------------------------


def test_header_only_bars_count_as_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    u = {"eval": EVAL, "top": TOP}
    _universe(tmp_path, monkeypatch, ["KS11", *EVAL, *TOP], u)
    (tmp_path / "bars" / f"{EVAL[1]}.csv").write_text("date,open,high,low,close,volume\n")
    with pytest.raises(SystemExit, match=EVAL[1]):  # 헤더만 있는 CSV → 조용히 빠지지 않고 중단
        mx.universe_tickers("A")


def test_cost_model_is_part_of_cache_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    monkeypatch.setattr(mx, "BARS_DIR", tmp_path)
    (tmp_path / "KS11.csv").write_text("a\n")
    th = mx.template_hash(TEMPLATE)
    fp = mx.experiment_fingerprint("logit", "A", TEMPLATE, [], None)
    real = mx.CostModel
    monkeypatch.setattr(mx, "CostModel", lambda: replace(real(), sell_tax_bps=15.0))
    assert mx.template_hash(TEMPLATE) != th
    assert mx.experiment_fingerprint("logit", "A", TEMPLATE, [], None) != fp


# -- Codex 7차 리뷰 반영 ----------------------------------------------------------------


def test_interrupted_refresh_must_resume_with_refresh() -> None:
    mx.check_fetch_resume({}, refresh=False, delisted=False)  # 처음
    done = {"fetch_complete": True, "fetch_mode": "refresh", "fetch_delisted": True}
    mx.check_fetch_resume(done, refresh=False, delisted=False)  # 완료된 뒤엔 자유
    # 일반 fetch 가 중단·필수 실패로 미완료여도 전체 재수신(--refresh)으로만 재개 (Codex 25차)
    with pytest.raises(SystemExit, match="--refresh"):
        mx.check_fetch_resume({"fetch_complete": False, "fetch_mode": "normal"}, False, False)
    mx.check_fetch_resume({"fetch_complete": False, "fetch_mode": "normal"}, True, False)
    interrupted = {"fetch_complete": False, "fetch_mode": "refresh", "fetch_delisted": False}
    with pytest.raises(SystemExit, match="--refresh"):
        mx.check_fetch_resume(interrupted, refresh=False, delisted=False)
    mx.check_fetch_resume(interrupted, refresh=True, delisted=False)


def test_interrupted_delisted_fetch_must_resume_with_delisted() -> None:
    interrupted = {"fetch_complete": False, "fetch_mode": "refresh", "fetch_delisted": True}
    with pytest.raises(SystemExit, match="--delisted"):  # --refresh 만으로는 부족
        mx.check_fetch_resume(interrupted, refresh=True, delisted=False)
    normal = {"fetch_complete": False, "fetch_mode": "normal", "fetch_delisted": True}
    with pytest.raises(SystemExit, match="--delisted"):
        mx.check_fetch_resume(normal, refresh=False, delisted=False)
    mx.check_fetch_resume(interrupted, refresh=True, delisted=True)
    mx.check_fetch_resume(normal, refresh=True, delisted=True)  # 더 넓은 범위 재개는 허용


def test_missing_prerequisites_are_undetermined_not_rejected() -> None:
    assert mx.final_verdict(["latency 측정 없음"], True, True, True, False) == "판정 불가"
    assert mx.final_verdict(["mitra/A/N10 예측 없음"], False, False, False, False) == "판정 불가"
    assert mx.final_verdict([], True, True, True, True) == "채택"
    assert mx.final_verdict([], True, False, True, True) == "기각"


def test_evaluate_without_eval_bars_writes_undetermined_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    u = {"eval": EVAL, "top": TOP}
    _universe(tmp_path, monkeypatch, ["KS11", *EVAL[:-1], *TOP], u)  # 평가 종목 하나 일봉 없음
    monkeypatch.setattr(mx, "ROOT", tmp_path)
    args = argparse.Namespace(watchlist="none", hf_model="m2")
    mx.cmd_evaluate(args)  # FileNotFoundError 없이 보고서 작성
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "**판정 불가**" in report and EVAL[-1] in report and "기각" not in report.split("미비")[0]


# -- Codex 9차 리뷰 반영 ----------------------------------------------------------------


def test_checkpoint_id_tracks_weight_contents(tmp_path: Path) -> None:
    ckpt = tmp_path / "ckpt"
    ckpt.mkdir()
    (ckpt / "config.json").write_text('{"dim": 64}')
    (ckpt / "model.safetensors").write_bytes(b"weights-v1")
    path, cid = mx.resolve_checkpoint(str(ckpt))
    assert path == str(ckpt) and mx.resolve_checkpoint(str(ckpt))[1] == cid
    (ckpt / "model.safetensors").write_bytes(b"weights-v2")  # 같은 경로, 가중치 교체
    assert mx.resolve_checkpoint(str(ckpt))[1] != cid
    (ckpt / "config.json").unlink()
    with pytest.raises(SystemExit, match=r"config\.json"):
        mx.resolve_checkpoint(str(ckpt))


# -- Codex 10차 리뷰 반영 ---------------------------------------------------------------


def test_universe_b_rejected_after_refresh_without_delisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    u = {"eval": EVAL, "top": TOP, "delisted": ["111110"], "unavailable": [], "b_stale": True}
    _universe(tmp_path, monkeypatch, ["KS11", *EVAL, *TOP, "111110"], u)
    assert mx.universe_tickers("A")  # A 는 영향 없음
    with pytest.raises(SystemExit, match="--refresh --delisted"):
        mx.universe_tickers("B")


def test_checkpoint_id_tracks_inference_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ckpt = tmp_path / "ckpt"
    ckpt.mkdir()
    (ckpt / "config.json").write_text("{}")
    (ckpt / "model.safetensors").write_bytes(b"w")
    monkeypatch.setattr(mx, "mitra_impl", lambda: "autogluon.tabular=1.6.3,torch=2.13.0")
    old = mx.resolve_checkpoint(str(ckpt))[1]
    monkeypatch.setattr(mx, "mitra_impl", lambda: "autogluon.tabular=1.7.0,torch=2.13.0")
    assert mx.resolve_checkpoint(str(ckpt))[1] != old  # 같은 가중치, 다른 AutoGluon


# -- Codex 11차 리뷰 반영 ---------------------------------------------------------------


def test_save_series_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "005930.csv"
    mx.save_series(_series("005930", 30, 1), path)
    good = path.read_text()

    def boom(*_a: Any) -> None:
        raise OSError("boom")

    # 쓰기 완료 전 실패 → 기존 파일 그대로, 잘린 파일이 정상 경로에 남지 않음
    monkeypatch.setattr(mx.os, "replace", boom)
    with pytest.raises(OSError):
        mx.save_series(_series("005930", 60, 2), path)
    assert path.read_text() == good


def test_impl_key_is_part_of_cache_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mx, "BARS_DIR", tmp_path)
    (tmp_path / "KS11.csv").write_text("a\n")
    th = mx.template_hash(TEMPLATE)
    fp = mx.experiment_fingerprint("logit", "A", TEMPLATE, [], None)
    monkeypatch.setattr(mx, "impl_key", lambda: "changed-strategy-code")
    assert mx.template_hash(TEMPLATE) != th
    assert mx.experiment_fingerprint("logit", "A", TEMPLATE, [], None) != fp


# -- Codex 12차 리뷰 반영 ---------------------------------------------------------------


def test_impl_key_covers_whole_runtime_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    import short_trading_bot

    src = Path(short_trading_bot.__file__).parent
    fake = tmp_path / "short_trading_bot"
    for pkg in mx.IMPL_PACKAGES:
        shutil.copytree(src / pkg, fake / pkg)
    (fake / "__init__.py").write_text("")
    monkeypatch.setattr(short_trading_bot, "__file__", str(fake / "__init__.py"))
    before = mx.impl_key()
    # 셋업 구성 경로의 모듈(팩토리·포지션·템플릿)이 바뀌어도 키가 바뀐다
    for rel in ("domain/factory.py", "domain/position.py", "strategy/templates.py"):
        target = fake / rel
        target.write_text(target.read_text() + "\n# changed\n")
        assert mx.impl_key() != before
        before = mx.impl_key()


def test_b_stale_after_top_only_or_bars_only_refresh() -> None:
    with_b = {"delisted": ["111110"], "b_stale": False}
    assert mx.b_stale_after(with_b, refresh=False, refresh_universe=True, delisted=False)
    assert mx.b_stale_after(with_b, refresh=True, refresh_universe=False, delisted=False)
    assert not mx.b_stale_after(with_b, refresh=True, refresh_universe=True, delisted=True)
    assert not mx.b_stale_after(with_b, refresh=False, refresh_universe=True, delisted=True)
    stale = {**with_b, "b_stale": True}
    assert mx.b_stale_after(stale, refresh=False, refresh_universe=False, delisted=True)  # 유지
    # 기존 일봉을 다시 받지 않는 --refresh-universe --delisted 는 이전 표시를 해제하지 못함
    assert mx.b_stale_after(stale, refresh=False, refresh_universe=True, delisted=True)
    assert not mx.b_stale_after(stale, refresh=True, refresh_universe=False, delisted=True)
    assert not mx.b_stale_after({}, refresh=True, refresh_universe=True, delisted=False)  # B 없음


# -- Codex 13차 리뷰 반영 ---------------------------------------------------------------


def test_interrupted_refresh_universe_must_resume_with_it() -> None:
    interrupted = {"fetch_complete": False, "fetch_mode": "refresh", "fetch_delisted": False,
                   "fetch_refresh_universe": True}
    with pytest.raises(SystemExit, match="--refresh-universe"):
        mx.check_fetch_resume(interrupted, refresh=True, delisted=False, refresh_universe=False)
    mx.check_fetch_resume(interrupted, refresh=True, delisted=False, refresh_universe=True)


def test_universe_rejects_bars_newer_than_ks11(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    u = {"eval": EVAL, "top": TOP}
    _universe(tmp_path, monkeypatch, ["KS11", *EVAL, *TOP], u)  # 전부 2016-01-04 에 끝남
    assert mx.universe_tickers("A")
    (tmp_path / "bars" / f"{TOP[0]}.csv").write_text(
        "date,open,high,low,close,volume\n2016-01-04,1,1,1,1,1\n2016-01-05,1,1,1,1,1\n"
    )  # 새로 받은 종목만 최신, KS11 은 옛 시점
    with pytest.raises(SystemExit, match="KS11"):
        mx.universe_tickers("A")


# -- Codex 14차 리뷰 반영 ---------------------------------------------------------------


def test_effective_filtered_counts_only_executed_base_entries() -> None:
    d = [date(2016, 3, k) for k in range(1, 8)]
    base = mx.RunStats("현행", 3, 0, 0.0, 0.0, {}, {},
                       entries=frozenset({("A", d[0]), ("B", d[1]), ("C", d[2])}))
    # 게이트가 5건을 막았지만 현행에서 실제 체결된 건 2건뿐(나머지는 현금 부족 등으로 미체결)
    arm = mx.RunStats("게이트", 1, 5, 0.0, 0.0, {}, {},
                      blocked=frozenset({("A", d[0]), ("B", d[1]), ("D", d[3]), ("E", d[4]),
                                         ("F", d[5])}))
    assert mx.effective_filtered(arm, base) == 2


def test_read_preds_skips_torn_rows(tmp_path: Path) -> None:
    path = tmp_path / "p.csv"
    good = [_pred("A", date(2016, 1, 4), date(2016, 1, 4)),
            _pred("B", date(2016, 1, 5), date(2016, 1, 4))]
    mx._append_preds(path, good)
    with path.open("a") as f:
        f.write("C,2016-01-0")  # 쓰기 도중 중단 — 개행 없이 찢어진 행
    rows = mx.read_preds(path)  # 예외 없이 정상 행만
    assert [(r.ticker, r.day) for r in rows] == [("A", date(2016, 1, 4)), ("B", date(2016, 1, 5))]
    # 재개 시 정상 행만으로 다시 쓰면 이어 쓰는 행이 찢어진 행과 붙지 않는다
    mx._rewrite_preds(path, rows)
    mx._append_preds(path, [_pred("C", date(2016, 1, 11), date(2016, 1, 11))])
    assert [r.ticker for r in mx.read_preds(path)] == ["A", "B", "C"]


# -- Codex 15차 리뷰 반영 ---------------------------------------------------------------


def test_read_preds_drops_unterminated_last_row_even_if_parseable(tmp_path: Path) -> None:
    path = tmp_path / "p.csv"
    mx._append_preds(path, [_pred("A", date(2016, 1, 4), date(2016, 1, 4))])
    with path.open("a") as f:  # 마지막 필드(net_ret) 도중 끊김 — "-0.01" 은 유효한 실수로 파싱됨
        f.write("B,2016-01-05,2016-01-04,0.5,0.5,300,1,-0.01")
    assert [r.ticker for r in mx.read_preds(path)] == ["A"]


def test_check_delisted_list_requires_cumulative_and_minimum_size() -> None:
    prev = [f"{i:05d}0" for i in range(mx.MIN_DELISTED * 3)]  # 60% 도 최소 규모 이상이 되게
    assert mx.check_delisted_list([*prev, "999990"], prev) == [*prev, "999990"]  # 누적 증가 OK
    with pytest.raises(SystemExit, match="<"):
        mx.check_delisted_list([], prev)
    # 60% 만 돌아온 경우(절반 이상이라도) — 기존 종목이 빠졌으니 누적성 위반으로 거부
    with pytest.raises(SystemExit, match="빠짐"):
        mx.check_delisted_list(prev[: int(len(prev) * 0.6)], prev)
    with pytest.raises(SystemExit, match="빠짐"):  # 크기는 같아도 구성이 바뀌면 거부
        mx.check_delisted_list([*prev[1:], "999990"], prev)
    with pytest.raises(SystemExit, match="<"):  # 최초 조회도 현실적 최소 규모 필요
        mx.check_delisted_list(["000010"], None)
    assert len(mx.check_delisted_list(prev, None)) == len(prev)


def test_universe_b_rejects_when_no_usable_delisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    u = {"eval": EVAL, "top": TOP, "delisted": ["111110"], "unavailable": ["111110"]}
    _universe(tmp_path, monkeypatch, ["KS11", *EVAL, *TOP], u)
    with pytest.raises(SystemExit, match="상폐 종목이 0개"):
        mx.universe_tickers("B")
    import json

    few = {"eval": EVAL, "top": TOP, "delisted": ["111110"], "unavailable": []}
    (tmp_path / "universe.json").write_text(
        json.dumps({"fetch_complete": True, "schema": mx.UNIVERSE_SCHEMA, **few})
    )
    (tmp_path / "bars" / "111110.csv").write_text(
        "date,open,high,low,close,volume\n2016-01-04,1,1,1,1,1\n"
    )
    with pytest.raises(SystemExit, match="1개 <"):  # 한 종목뿐인 B 도 거부
        mx.universe_tickers("B")


# -- Codex 17차 리뷰 반영 ---------------------------------------------------------------


def test_top_by_marcap_sorts_and_requires_marcap() -> None:
    rows = [{"Code": f"{i:05d}0", "Marcap": float(i)} for i in range(1, 151)]
    rows.append({"Code": "999995", "Marcap": 1e20})  # 우선주는 제외
    top = mx.top_by_marcap(reversed(rows))  # 제공처 순서와 무관하게 시총 순
    assert len(top) == mx.UNIVERSE_TOP and top[0] == "001500" and "999995" not in top
    no_cap = [{"Code": r["Code"]} for r in rows]  # 컬럼 누락·이름 변경
    with pytest.raises(SystemExit, match="Marcap"):
        mx.top_by_marcap(no_cap)
    bad = [*rows[:-1], {"Code": "888880", "Marcap": float("nan")}]
    with pytest.raises(SystemExit, match="비정상"):
        mx.top_by_marcap(bad)
    with pytest.raises(SystemExit, match="< 100"):
        mx.top_by_marcap(rows[:50])


# -- Codex 18차 리뷰 반영 ---------------------------------------------------------------


def test_universe_rejects_legacy_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    u = {"eval": EVAL, "top": TOP}
    _universe(tmp_path, monkeypatch, ["KS11", *EVAL, *TOP], u)
    assert mx.universe_tickers("A")
    # 형식 버전 표시가 없는 이전 목록(행 순서로 뽑혔을 수 있음) → 거부
    (tmp_path / "universe.json").write_text(json.dumps({"fetch_complete": True, **u}))
    with pytest.raises(SystemExit, match="지우고"):
        mx.universe_tickers("A")


# -- Codex 19차 리뷰 반영 ---------------------------------------------------------------


def test_write_atomic_keeps_previous_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "universe.json"
    mx.write_atomic(path, '{"top": ["000010"]}')
    assert path.read_text() == '{"top": ["000010"]}'

    def boom(*_a: Any) -> None:
        raise OSError("power loss")

    monkeypatch.setattr(mx.os, "replace", boom)  # 교체 직전 중단
    with pytest.raises(OSError):
        mx.write_atomic(path, '{"top": [')
    assert path.read_text() == '{"top": ["000010"]}'  # 잘린 JSON 이 남지 않음


# -- Codex 20차 리뷰 반영 ---------------------------------------------------------------


def test_write_atomic_fsyncs_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import stat

    synced: list[bool] = []  # fsync 된 fd 가 디렉터리인지
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(mx.os, "fsync", spy)
    mx.write_atomic(tmp_path / "universe.json", "{}")
    assert synced == [False, True]  # 파일 내용 → rename 을 담은 디렉터리 순
    synced.clear()
    mx.save_series(_series("005930", 5, 1), tmp_path / "005930.csv")
    assert synced == [False, True]


# -- Codex 21차 리뷰 반영 ---------------------------------------------------------------


def test_fsync_dir_propagates_real_io_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                             ) -> None:
    def fail(err: int) -> Any:
        def _fsync(fd: int) -> None:
            raise OSError(err, "x")
        return _fsync

    monkeypatch.setattr(mx.os, "fsync", fail(errno.EINVAL))  # 디렉터리 fsync 미지원 → 무시
    mx._fsync_dir(tmp_path)
    monkeypatch.setattr(mx.os, "fsync", fail(errno.EIO))  # 실제 저장장치 오류 → 전파
    with pytest.raises(OSError):
        mx._fsync_dir(tmp_path)


def test_verify_preds_rejects_csv_that_diverged_from_marker(tmp_path: Path) -> None:
    path = tmp_path / "p.csv"
    rows = [_pred("A", date(2016, 1, 4), date(2016, 1, 4)),
            _pred("B", date(2016, 1, 5), date(2016, 1, 4)),
            _pred("C", date(2016, 1, 11), date(2016, 1, 11))]
    mx._append_preds(path, rows)
    marker = {"rows": 3, "queries": 3, "blocks": 2, "sha256": mx.preds_digest(path)}
    assert mx.verify_preds(path, mx.read_preds(path), marker) is None
    # 해시 없는 옛 표시는 거부
    legacy = {k: v for k, v in marker.items() if k != "sha256"}
    assert mx.verify_preds(path, mx.read_preds(path), legacy) is not None
    # 전원 차단으로 뒷부분이 유실된 CSV — 표시는 남아 있어도 거부
    mx._rewrite_preds(path, rows[:2])
    assert "다름" in (mx.verify_preds(path, mx.read_preds(path), marker) or "")
    # 해시가 우연히 맞더라도(표시가 부분 CSV 로 기록된 경우) 행·블록 수로 거부
    partial = {**marker, "sha256": mx.preds_digest(path)}
    assert mx.verify_preds(path, mx.read_preds(path), partial) is not None


# -- Codex 22차 리뷰 반영 ---------------------------------------------------------------


def test_empty_prediction_plan_is_a_verifiable_complete_result(tmp_path: Path) -> None:
    path = tmp_path / "p.csv"
    mx._rewrite_preds(path, [])  # 모든 블록이 지지 부족 — 예측 행 없음
    rows = mx.read_preds(path)
    assert rows == [] and mx.is_complete(rows, {})
    marker = {"rows": 0, "queries": 0, "blocks": 0, "sha256": mx.preds_digest(path)}
    assert mx.verify_preds(path, rows, marker) is None


# -- Codex 23차 리뷰 반영 ---------------------------------------------------------------


def test_required_failure_keeps_refresh_fetch_incomplete() -> None:
    u: dict[str, Any] = {"fetch_complete": False, "fetch_mode": "refresh", "fetch_delisted": False,
                         "delisted": ["999990"]}
    assert mx.finish_fetch(u, [mx.KS11, "999990"]) == [mx.KS11]  # 상폐 실패는 필수 아님
    assert u["fetch_complete"] is False
    with pytest.raises(SystemExit, match="--refresh"):  # 옵션 없는 재실행으로 혼합 스냅샷 승인 불가
        mx.check_fetch_resume(u, False, False)
    assert mx.finish_fetch(u, ["999990"]) == [] and u["fetch_complete"] is True


def test_impl_key_covers_experiment_script_source(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "exp.py"
    script.write_text("A = 1\n")
    monkeypatch.setattr(mx, "SCRIPT_FILE", script)
    before = mx.impl_key()
    script.write_text("A = 2\n")  # 예측 구현 수정
    assert mx.impl_key() != before


# -- Codex 26차 리뷰 반영 ---------------------------------------------------------------


def test_new_failure_of_previously_fetched_delisted_keeps_fetch_incomplete() -> None:
    u: dict[str, Any] = {"delisted": ["111110", "222220", "333330"]}
    # 222220 은 이전 완료 fetch 에서 정상 수신됨, 333330 은 목록에 새로 들어온 종목
    assert mx.finish_fetch(u, ["222220", "333330"], prior_available={"111110", "222220"}) == [
        "222220"
    ]
    assert u["fetch_complete"] is False  # B 를 조용히 축소하지 않는다
    assert mx.finish_fetch(u, ["333330"], prior_available={"111110", "222220"}) == []


def test_current_tickers_lagging_ks11_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    u = {"eval": EVAL, "top": TOP}
    _universe(tmp_path, monkeypatch, ["KS11", *EVAL, *TOP], u)
    days = [date(2016, 1, 4) + timedelta(days=i) for i in range(10)]
    rows = "".join(f"{d.isoformat()},1,1,1,1,1\n" for d in days)
    header = "date,open,high,low,close,volume\n2010-01-04,1,1,1,1,1\n"
    for t in ["KS11", *EVAL, *TOP]:
        (tmp_path / "bars" / f"{t}.csv").write_text(header + rows)
    mx.universe_tickers("A")  # 전부 최신 → 통과
    (tmp_path / "bars" / f"{TOP[3]}.csv").write_text(header + rows.splitlines(True)[0])
    assert mx.lagging_codes([*EVAL, *TOP]) == [TOP[3]]  # 중간에서 잘린 응답
    with pytest.raises(SystemExit, match=TOP[3]):
        mx.universe_tickers("A")


# -- Codex 27차 리뷰 반영 ---------------------------------------------------------------


def test_delisted_baseline_survives_failed_retries() -> None:
    # 완료된 fetch: 111110·222220 정상 수신, 333330 조회 불가 → 기준 확정
    u: dict[str, Any] = {"delisted": ["111110", "222220", "333330"], "unavailable": ["333330"],
                         "fetch_complete": True, "delisted_failed_once": ["333330"]}
    assert mx.finish_fetch(u, ["333330"], mx.available_baseline(u), delisted_fetched=True) == []
    assert u["delisted_available"] == ["111110", "222220"]
    # 1차 재시도: 222220 이 일시 실패 → unavailable 에 들어가지만 fetch 는 미완료
    u["unavailable"] = ["222220", "333330"]
    assert mx.finish_fetch(u, ["222220", "333330"], mx.available_baseline(u), True) == ["222220"]
    # 2차 재시도: 기준은 미완료 실행이 바꾼 unavailable 로 깎이지 않는다 → 여전히 필수 실패
    assert mx.available_baseline(u) == {"111110", "222220"}
    assert mx.finish_fetch(u, ["222220", "333330"], mx.available_baseline(u), True) == ["222220"]
    assert u["fetch_complete"] is False


def test_prefix_truncated_mandatory_history_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = {c: "2005-01-03" for c in TOP} | {TOP[0]: "2016-01-04"}  # TOP[0] 은 신규 상장
    u = {"eval": EVAL, "top": TOP, "top_listed": listed}
    _universe(tmp_path, monkeypatch, ["KS11", *EVAL, *TOP], u)
    mx.universe_tickers("A")  # 전부 2010-01-04 부터 → 통과
    # 신규 상장 시총 상위 종목은 (상장일 기준으로) 늦게 시작해도 정상
    (tmp_path / "bars" / f"{TOP[0]}.csv").write_text(
        "date,open,high,low,close,volume\n2016-01-04,1,1,1,1,1\n"
    )
    mx.universe_tickers("A")
    # 평가 종목이 2017 부터만 온 응답 → 앞부분 잘림
    (tmp_path / "bars" / f"{EVAL[1]}.csv").write_text(
        "date,open,high,low,close,volume\n2015-06-01,1,1,1,1,1\n2016-01-04,1,1,1,1,1\n"
    )
    assert mx.late_start_codes(["KS11", *EVAL]) == [EVAL[1]]
    with pytest.raises(SystemExit, match=EVAL[1]):
        mx.universe_tickers("A")


# -- Codex 28차 리뷰 반영 ---------------------------------------------------------------


def test_restored_delisted_baseline_is_stored_before_fetch_starts() -> None:
    # delisted_available 이 없는 이전 형식의 완료 캐시
    u: dict[str, Any] = {"fetch_complete": True, "delisted": ["111110", "222220"],
                         "unavailable": ["222220"]}
    assert mx.available_baseline(u) == {"111110"}
    assert u["delisted_available"] == ["111110"]  # 진행 중 표시와 함께 영속화될 값
    u["fetch_complete"] = False  # 이번 실행이 중단됨
    assert mx.available_baseline(u) == {"111110"}  # 재시도에서도 기준 유지


# -- Codex 29차 리뷰 반영 ---------------------------------------------------------------


def test_legacy_incomplete_delisted_cache_without_baseline_is_rejected() -> None:
    fresh: dict[str, Any] = {"delisted": ["111110"]}  # 상폐 조회를 완료한 적 없음
    assert mx.available_baseline(fresh) == set() and fresh["delisted_available"] == []
    legacy = {"fetch_complete": False, "delisted": ["111110"], "unavailable": ["111110"]}
    with pytest.raises(SystemExit, match="복원할 수 없음"):
        mx.available_baseline(legacy)


def test_coverage_shrunk_detects_partial_responses(tmp_path: Path) -> None:
    path = tmp_path / "111110.csv"
    path.write_text("date,open,high,low,close,volume\n"
                    "2012-01-02,1,1,1,1,1\n2013-01-02,1,1,1,1,1\n2014-06-30,1,1,1,1,1\n")

    def series(*days: date) -> Any:
        z = np.ones(len(days))
        return mx.Series("111110", list(days), z, z, z, z, z)

    d1, d2, d3 = date(2012, 1, 2), date(2013, 1, 2), date(2014, 6, 30)
    assert not mx.coverage_shrunk(path, series(d1, d2, d3))
    assert not mx.coverage_shrunk(path, series(d1, d2, d3, date(2014, 7, 1)))  # 늘어난 건 정상
    assert mx.coverage_shrunk(path, series(d2, d3))  # 앞 잘림
    assert mx.coverage_shrunk(path, series(d1, d2))  # 뒤 잘림
    assert mx.coverage_shrunk(path, series(d1, d3))  # 양 끝은 같고 중간 구간 누락 (Codex 31차)


# -- Codex 30차 리뷰 반영 ---------------------------------------------------------------


def test_orphan_bars_force_full_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mx, "BARS_DIR", tmp_path / "bars")
    assert not mx.orphan_bars({})  # 처음 — 캐시 없음
    (tmp_path / "bars").mkdir()
    (tmp_path / "bars" / "005930.csv").write_text("date,open,high,low,close,volume\n")
    assert mx.orphan_bars({})  # 메타데이터 없이 남은 일봉 → 전체 새로고침 강제
    assert not mx.orphan_bars({"top": ["000660"]})


class _FakeFdr:
    """cmd_fetch 통합 테스트용 FinanceDataReader 대역. ``bars[code]`` = (시작, 끝) 또는 None(실패)."""

    DELISTED: ClassVar[list[str]] = [f"8{i:04d}0" for i in range(mx.MIN_DELISTED)]

    def __init__(self) -> None:
        full = (date(2010, 1, 4), date(2016, 3, 31))
        self.bars: dict[str, tuple[date, date] | None] = {
            c: full for c in [mx.KS11, *EVAL, *TOP]
        }
        self.bars.update({c: (date(2010, 1, 4), date(2014, 6, 30)) for c in self.DELISTED})

    def StockListing(self, name: str) -> Any:  # FDR API 이름 그대로
        import pandas as pd

        if name == "KOSPI":
            return pd.DataFrame([{"Code": c, "Marcap": 1e12 - i} for i, c in enumerate(TOP)])
        if name == "KRX-DESC":
            return pd.DataFrame([{"Code": c, "ListingDate": "2005-01-03"} for c in self.bars])
        return pd.DataFrame([{"Symbol": c, "Name": "x", "Market": "KOSDAQ",
                              "DelistingDate": "2014-07-01", "SecuGroup": "주권", "ListingDate": "2005-01-03"}
                             for c in self.DELISTED])

    def DataReader(self, symbol: str, start: str) -> Any:
        import pandas as pd

        span = self.bars.get(symbol.removeprefix("KRX-DELISTING:"))
        if span is None:
            return pd.DataFrame()
        idx = pd.bdate_range(span[0], span[1])
        one = [1.0] * len(idx)
        return pd.DataFrame({"Open": one, "High": one, "Low": one, "Close": one, "Volume": one},
                            index=idx)


def _run_fetch(fake: _FakeFdr, monkeypatch: pytest.MonkeyPatch, *flags: str) -> dict[str, Any]:
    import json

    monkeypatch.setitem(sys.modules, "FinanceDataReader", fake)
    ns = mx.main.__globals__["argparse"].Namespace(
        delisted="--delisted" in flags, refresh="--refresh" in flags,
        refresh_universe="--refresh-universe" in flags)
    mx.cmd_fetch(ns)
    return dict(json.loads(mx.UNIVERSE_FILE.read_text()))


@pytest.fixture
def fetch_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeFdr:
    monkeypatch.setattr(mx, "ROOT", tmp_path)
    monkeypatch.setattr(mx, "BARS_DIR", tmp_path / "bars")
    monkeypatch.setattr(mx, "UNIVERSE_FILE", tmp_path / "universe.json")
    return _FakeFdr()


def test_fetch_rejects_shrunk_response_for_existing_current_ticker(
    fetch_env: _FakeFdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    u = _run_fetch(fetch_env, monkeypatch, "--delisted")
    assert u["fetch_complete"] is True and len(mx.universe_tickers("B")) > len(TOP)
    # 새로고침에서 시총 상위 종목이 앞부분이 잘린 응답(현재까지는 끝남)을 돌려줌
    fetch_env.bars[TOP[5]] = (date(2014, 1, 2), date(2016, 3, 31))
    # 이전에 정상 수신된 상폐 종목은 뒷부분이 잘린 응답
    fetch_env.bars[_FakeFdr.DELISTED[7]] = (date(2010, 1, 4), date(2012, 1, 2))
    u = _run_fetch(fetch_env, monkeypatch, "--refresh", "--delisted")
    assert u["fetch_complete"] is False
    assert set(u["required_failed"]) == {TOP[5], _FakeFdr.DELISTED[7]}
    # 옛 일봉은 커버리지 기준으로 보존 (축소 응답으로 덮지 않음)
    assert mx._first_day(mx.BARS_DIR / f"{TOP[5]}.csv") == date(2010, 1, 4)
    with pytest.raises(SystemExit, match="--refresh"):  # 옵션 없는 재개 거부
        _run_fetch(fetch_env, monkeypatch, "--delisted")
    fetch_env.bars[TOP[5]] = (date(2010, 1, 4), date(2016, 3, 31))  # 복구
    fetch_env.bars[_FakeFdr.DELISTED[7]] = (date(2010, 1, 4), date(2014, 6, 30))
    u = _run_fetch(fetch_env, monkeypatch, "--refresh", "--delisted")
    assert u["fetch_complete"] is True and u["required_failed"] == []


def test_fetch_without_metadata_refreshes_leftover_bars(
    fetch_env: _FakeFdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_fetch(fetch_env, monkeypatch)
    old = mx.BARS_DIR / f"{EVAL[0]}.csv"
    old.write_text("date,open,high,low,close,volume\n2010-01-04,1,1,1,1,1\n")  # 옛 시점 일봉
    mx.UNIVERSE_FILE.unlink()  # 메타데이터만 삭제
    _run_fetch(fetch_env, monkeypatch)  # 옵션 없이 실행해도 전체 새로고침
    assert mx._last_day(old) == date(2016, 3, 31)


# -- Codex 31차 리뷰 반영 ---------------------------------------------------------------


def test_former_top_ticker_moved_to_delisted_stays_protected(
    fetch_env: _FakeFdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    u = _run_fetch(fetch_env, monkeypatch, "--delisted")
    assert u["fetch_complete"] is True
    gone = TOP[-1]  # 시총 상위였다가 상폐됨 → 새 KOSPI 목록에서 빠지고 상폐 목록에 들어감
    new_top = [f"7{i:04d}0" for i in range(3)]
    for c in new_top:
        fetch_env.bars[c] = (date(2010, 1, 4), date(2016, 3, 31))
    real_listing = fetch_env.StockListing

    def listing(name: str) -> Any:
        import pandas as pd

        if name == "KOSPI":
            codes = [*new_top, *TOP[:-1]]
            return pd.DataFrame([{"Code": c, "Marcap": 1e12 - i} for i, c in enumerate(codes)])
        df = real_listing(name)
        extra = {"Symbol": gone, "Name": "x", "Market": "KOSPI", "DelistingDate": "2016-03-31",
                 "SecuGroup": "주권", "ListingDate": "2005-01-03"}
        return pd.concat([df, pd.DataFrame([extra])], ignore_index=True)

    monkeypatch.setattr(fetch_env, "StockListing", listing)
    fetch_env.bars[gone] = None  # 일시 장애로 빈 응답
    u = _run_fetch(fetch_env, monkeypatch, "--refresh-universe", "--delisted")
    assert gone in u["delisted"] and gone not in u["top"]
    assert u["fetch_complete"] is False and gone in u["required_failed"]  # B 를 조용히 줄이지 않음
    assert (mx.BARS_DIR / f"{gone}.csv").exists()  # 옛 일봉 보존
    assert gone in u["delisted_available"]  # 재시도에서도 보호


# -- Codex 32차 리뷰 반영 ---------------------------------------------------------------


def test_parse_delisted_requires_security_type_column() -> None:
    import pandas as pd

    rows = [{"Symbol": "111110", "Name": "a", "Market": "KOSPI", "DelistingDate": "2014-07-01",
             "SecuGroup": "주권", "ListingDate": "2005-01-03"},
            {"Symbol": "222220", "Name": "b", "Market": "KOSPI", "DelistingDate": "2014-07-01",
             "SecuGroup": "ETF", "ListingDate": "2005-01-03"}]
    assert mx.parse_delisted(pd.DataFrame(rows)) == {"111110": ["2005-01-03", "2014-07-01"]}
    with pytest.raises(SystemExit, match="SecuGroup"):
        mx.parse_delisted(pd.DataFrame(rows).drop(columns=["SecuGroup"]))


def test_former_top_stays_protected_across_separate_universe_and_delisted_refresh(
    fetch_env: _FakeFdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pandas as pd

    _run_fetch(fetch_env, monkeypatch, "--delisted")
    gone = TOP[-1]
    new_top = [f"7{i:04d}0" for i in range(3)]
    for c in new_top:
        fetch_env.bars[c] = (date(2010, 1, 4), date(2016, 3, 31))
    real_listing = fetch_env.StockListing
    delisted_now = [False]

    def listing(name: str) -> Any:
        if name == "KOSPI":
            codes = [*new_top, *TOP[:-1]]
            return pd.DataFrame([{"Code": c, "Marcap": 1e12 - i} for i, c in enumerate(codes)])
        df = real_listing(name)
        if not delisted_now[0]:
            return df
        extra = {"Symbol": gone, "Name": "x", "Market": "KOSPI", "DelistingDate": "2016-03-31",
                 "SecuGroup": "주권", "ListingDate": "2005-01-03"}
        return pd.concat([df, pd.DataFrame([extra])], ignore_index=True)

    monkeypatch.setattr(fetch_env, "StockListing", listing)
    u = _run_fetch(fetch_env, monkeypatch, "--refresh-universe")  # 목록만 먼저 갱신
    assert gone not in u["top"] and gone in u["former_mandatory"]
    delisted_now[0] = True  # 그 뒤 상폐 목록에 등장
    fetch_env.bars[gone] = None  # 일시 장애로 빈 응답
    u = _run_fetch(fetch_env, monkeypatch, "--refresh", "--delisted")
    assert u["fetch_complete"] is False and gone in u["required_failed"]
    assert (mx.BARS_DIR / f"{gone}.csv").exists()


# -- Codex 33차 리뷰 반영 ---------------------------------------------------------------


def test_fetch_rejects_cache_from_older_schema(
    fetch_env: _FakeFdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    _run_fetch(fetch_env, monkeypatch, "--delisted")
    u = json.loads(mx.UNIVERSE_FILE.read_text())
    # 이전 버전: 증권 유형 검증 없이 만든 상폐 목록, former_mandatory 기록 없음
    u["schema"] = mx.UNIVERSE_SCHEMA - 1
    mx.UNIVERSE_FILE.write_text(json.dumps(u))
    for flags in [(), ("--delisted",), ("--refresh", "--delisted"), ("--refresh-universe",)]:
        with pytest.raises(SystemExit, match="지우고"):
            _run_fetch(fetch_env, monkeypatch, *flags)
    with pytest.raises(SystemExit, match="지우고"):
        mx.universe_tickers("B")


def test_interrupted_first_fetch_can_resume(
    fetch_env: _FakeFdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_listing = fetch_env.StockListing

    def broken(name: str) -> Any:
        raise ConnectionError("down")

    monkeypatch.setattr(fetch_env, "StockListing", broken)
    with pytest.raises(ConnectionError):
        _run_fetch(fetch_env, monkeypatch)  # 첫 실행이 목록 조회 중 중단
    monkeypatch.setattr(fetch_env, "StockListing", real_listing)
    u = _run_fetch(fetch_env, monkeypatch, "--refresh")  # 형식 버전 거부 없이 재개
    assert u["fetch_complete"] is True and u["schema"] == mx.UNIVERSE_SCHEMA


# -- Codex 34차 리뷰 반영 ---------------------------------------------------------------


def test_new_delisted_ticker_ending_long_before_delisting_is_excluded(
    fetch_env: _FakeFdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    cut = _FakeFdr.DELISTED[4]
    fetch_env.bars[cut] = (date(2010, 1, 4), date(2012, 1, 2))  # 상폐일(2014-07-01)보다 2년 이상 이름
    u = _run_fetch(fetch_env, monkeypatch, "--delisted")
    assert u["fetch_complete"] is False and cut in u["required_failed"]  # 첫 실패는 재시도 요구
    u = _run_fetch(fetch_env, monkeypatch, "--refresh", "--delisted")  # 두 번째 실행도 실패 → 확정
    assert u["fetch_complete"] is True
    assert cut in u["unavailable"] and cut not in u["delisted_available"]  # 잘린 이력으로 채점 안 함
    assert not (mx.BARS_DIR / f"{cut}.csv").exists()
    assert _FakeFdr.DELISTED[5] in u["delisted_available"]  # 정상 응답은 그대로


# -- Codex 35차 리뷰 반영 ---------------------------------------------------------------


def test_worst_case_fill_for_right_censored_delisted_samples() -> None:
    def pred(ticker: str, p: float, label: int | None, ret: float | None) -> Any:
        return mx.Pred(ticker, date(2016, 3, 2), date(2016, 3, 2), p, 0.5, 400, label, ret)

    pool = [pred("A", 0.7, 1, 0.05), pred("A", 0.3, 0, -0.02)]
    censored = [pred("D", 0.8, None, None), pred("D", 0.2, None, None)]
    filled = mx.censored_fill(pool, censored, adverse=True)
    kept, removed = filled[2], filled[3]
    assert (kept.label, kept.net_ret) == (0, -1.0)  # 통과 신호 → 상폐 전액 손실
    # 걸러진 신호 → 관측 최대(0.05)도 1.3^N 도 아닌 +inf — 정리매매는 가격제한폭이 없음 (Codex 37차)
    assert removed.label == 1 and removed.net_ret == math.inf
    assert censored[0].label is None  # 원본은 그대로
    d = mx.discrimination(filled)
    assert d.kept_mean < d.removed_mean  # 최악 가정이면 ③ 이 뒤집힘 → B 는 판정 불가로 보고


def test_g0_b_is_undetermined_when_censoring_could_flip_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.setattr(mx, "UNIVERSE_FILE", tmp_path / "universe.json")
    (tmp_path / "universe.json").write_text(json.dumps({"delisted": ["800000"]}))
    monkeypatch.setattr(mx, "unavailable_note", lambda: "상폐 1종목")
    rng = np.random.default_rng(0)
    preds = []
    for i in range(400):  # 판별력 있는 표본: p 가 높을수록 라벨 1·수익 +
        p = float(rng.uniform())
        y = int(rng.uniform() < p)
        preds.append(mx.Pred("A", date(2017, 1, 2) + timedelta(days=i), date(2017, 1, 2), p, 0.5,
                             400, y, 0.03 if y else -0.03))
    censored = [mx.Pred("800000", date(2018, 1, 2) + timedelta(days=i), date(2018, 1, 2),
                        0.9 if i % 2 else 0.1, 0.5, 400, None, None) for i in range(400)]

    def fake(model: str, universe: str, n: int, template: Any, ckpt: Any) -> Any:
        return ([*preds, *censored] if universe == "B" else preds), "ok"

    monkeypatch.setattr(mx, "load_complete_preds", fake)
    lines: list[str] = []
    missing: list[str] = []
    mx.g0_section(lines, {}, "ckpt", missing)
    assert any("우측 검열" in m for m in missing)  # 명목상 통과해도 B 는 판정 불가
    missing.clear()
    monkeypatch.setattr(mx, "load_complete_preds",
                        lambda model, universe, n, t, c: (preds, "ok"))  # 검열 없음
    mx.g0_section([], {}, "ckpt", missing)
    assert not any("우측 검열" in m for m in missing)


def test_worst_case_holds_when_all_censored_samples_were_kept() -> None:
    def pred(p: float, label: int | None, ret: float | None) -> Any:
        return mx.Pred("A", date(2016, 3, 2), date(2016, 3, 2), p, 0.5, 400, label, ret)

    pool = [pred(0.9, 1, 0.10), pred(0.8, 1, 0.08), pred(0.2, 0, -0.30), pred(0.1, 0, -0.40)]
    d = mx.discrimination(mx.censored_fill(pool, [pred(0.7, None, None)], adverse=True))
    assert d.removed_mean < d.kept_mean  # 검열 표본이 통과 쪽뿐이면 -100% 로도 판정 가능
    assert mx._fmt(math.inf) == "+inf" and mx._fmt(math.nan) == "n/a"


# -- Codex 38차 리뷰 반영 ---------------------------------------------------------------


def test_censored_best_case_fill_mirrors_worst_case() -> None:
    def pred(p: float) -> Any:
        return mx.Pred("D", date(2016, 3, 2), date(2016, 3, 2), p, 0.5, 400, None, None)

    kept, removed = mx.censored_fill([], [pred(0.8), pred(0.2)], adverse=False)
    assert (kept.label, kept.net_ret) == (1, math.inf)
    assert (removed.label, removed.net_ret) == (0, -1.0)


def test_g0_b_nominal_fail_that_censoring_could_reverse_is_undetermined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.setattr(mx, "UNIVERSE_FILE", tmp_path / "universe.json")
    (tmp_path / "universe.json").write_text(json.dumps({"delisted": ["800000"]}))
    monkeypatch.setattr(mx, "unavailable_note", lambda: "상폐 1종목")
    rng = np.random.default_rng(1)
    preds = []
    for i in range(360):  # 판별력은 있지만 제외 평균이 통과 평균보다 살짝 높음 → 명목 실패
        p = float(rng.uniform())
        y = int(rng.uniform() < p)
        ret = (0.03 if y else -0.03) + (0.035 if p < 0.5 else 0.0)
        preds.append(mx.Pred("A", date(2017, 1, 2) + timedelta(days=i), date(2017, 1, 2), p, 0.5,
                             400, y, ret))
    censored = [mx.Pred("800000", date(2018, 1, 2), date(2018, 1, 2), 0.2, 0.5, 400, None, None)]

    def fake(model: str, universe: str, n: int, template: Any, ckpt: Any) -> Any:
        return ([*preds, *censored] if universe == "B" else preds), "ok"

    monkeypatch.setattr(mx, "load_complete_preds", fake)
    missing: list[str] = []
    mx.g0_section([], {}, "ckpt", missing)
    # 걸러진 검열 표본 하나가 -100% 면 제외 평균이 내려가 통과할 수 있음 → 기각 확정이 아니라 판정 불가
    assert any("우측 검열" in m for m in missing)


# -- Codex 39차 리뷰 반영 ---------------------------------------------------------------


def test_auc_bounds_follow_probability_rank_not_gate() -> None:
    import itertools

    def pred(p: float, base: float, label: int | None) -> Any:
        return mx.Pred("A", date(2016, 3, 2), date(2016, 3, 2), p, base, 400, label,
                       None if label is None else (0.01 if label else -0.01))

    pool = [pred(0.9, 0.5, 1), pred(0.6, 0.5, 0), pred(0.4, 0.5, 1), pred(0.1, 0.5, 0)]
    # 게이트 통과 여부가 p 순서와 어긋남: 낮은 p(0.3)가 낮은 base 로 통과, 높은 p(0.7)는 제외
    censored = [pred(0.3, 0.1, None), pred(0.7, 0.9, None), pred(0.5, 0.5, None)]
    lo, hi = mx.auc_bounds(pool, censored)
    brute = [mx.auc([*(q.p for q in pool), *(c.p for c in censored)],
                    [*(q.label for q in pool), *combo])
             for combo in itertools.product([0, 1], repeat=len(censored))]
    assert lo == pytest.approx(min(brute)) and hi == pytest.approx(max(brute))  # 전수 대조


# -- Codex 40차 리뷰 반영 ---------------------------------------------------------------


def test_first_delisted_fetch_mass_failure_requires_retry(
    fetch_env: _FakeFdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    down = _FakeFdr.DELISTED[: len(_FakeFdr.DELISTED) // 2]
    saved = {c: fetch_env.bars[c] for c in down}
    for c in down:
        fetch_env.bars[c] = None  # 최초 조회 중 일시 장애로 절반 실패
    u = _run_fetch(fetch_env, monkeypatch, "--delisted")
    assert u["fetch_complete"] is False  # 한 번의 실행으로 B 축소를 승인하지 않음
    fetch_env.bars.update(saved)  # 장애 복구 후 재시도
    u = _run_fetch(fetch_env, monkeypatch, "--refresh", "--delisted")
    assert u["fetch_complete"] is True and u["unavailable"] == []
    assert u["delisted_failed_once"] == []


def test_new_delisted_ticker_with_truncated_prefix_is_rejected(
    fetch_env: _FakeFdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    cut = _FakeFdr.DELISTED[6]
    fetch_env.bars[cut] = (date(2013, 1, 2), date(2014, 6, 30))  # 2005 상장인데 2013 부터만 옴
    _run_fetch(fetch_env, monkeypatch, "--delisted")
    u = _run_fetch(fetch_env, monkeypatch, "--refresh", "--delisted")
    assert u["fetch_complete"] is True and cut in u["unavailable"]
    assert not (mx.BARS_DIR / f"{cut}.csv").exists()


# -- Codex 41차 리뷰 반영 ---------------------------------------------------------------


def test_successful_retry_clears_failure_marker_before_interruption(
    fetch_env: _FakeFdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    flaky = _FakeFdr.DELISTED[3]
    saved = fetch_env.bars[flaky]
    fetch_env.bars[flaky] = None
    u = _run_fetch(fetch_env, monkeypatch, "--delisted")  # 1차 실패
    assert flaky in u["delisted_failed_once"]
    fetch_env.bars[flaky] = saved
    real_reader = fetch_env.DataReader
    last = _FakeFdr.DELISTED[-1]

    def interrupt(symbol: str, start: str) -> Any:
        if symbol.removeprefix("KRX-DELISTING:") == last:
            raise KeyboardInterrupt  # 재시도 도중(flaky 수신 이후) 중단
        return real_reader(symbol, start)

    monkeypatch.setattr(fetch_env, "DataReader", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _run_fetch(fetch_env, monkeypatch, "--refresh", "--delisted")
    u = json.loads(mx.UNIVERSE_FILE.read_text())
    assert flaky not in u["delisted_failed_once"]  # 성공이 영속화됨
    monkeypatch.setattr(fetch_env, "DataReader", real_reader)
    fetch_env.bars[flaky] = None  # 다음 재시도에서 다시 실패 → 새 첫 실패(두 번째로 세지 않음)
    u = _run_fetch(fetch_env, monkeypatch, "--refresh", "--delisted")
    assert u["fetch_complete"] is False and flaky in u["required_failed"]


def test_g0_excludes_illiquid_evaluation_only_queries(tmp_path: Path) -> None:
    path = tmp_path / "p.csv"
    rows = [mx.Pred("005930", date(2016, 3, 2), date(2016, 3, 2), 0.9, 0.5, 400, 0, -0.5,
                    liquid=False),
            mx.Pred("A", date(2016, 3, 2), date(2016, 3, 2), 0.9, 0.5, 400, 1, 0.1)]
    mx._append_preds(path, rows)
    back = mx.read_preds(path)
    assert [r.liquid for r in back] == [False, True]  # CSV 왕복 보존


def test_g0_section_scores_only_liquid_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.setattr(mx, "UNIVERSE_FILE", tmp_path / "universe.json")
    (tmp_path / "universe.json").write_text(json.dumps({"delisted": []}))
    monkeypatch.setattr(mx, "unavailable_note", lambda: "상폐 0종목")
    liquid = [mx.Pred("A", date(2017, 1, 2) + timedelta(days=i), date(2017, 1, 2), 0.5, 0.5,
                      400, i % 2, 0.01) for i in range(40)]
    illiquid = [mx.Pred("005930", date(2017, 1, 2) + timedelta(days=i), date(2017, 1, 2), 0.5,
                        0.5, 400, 1, 0.01, liquid=False) for i in range(25)]
    monkeypatch.setattr(mx, "load_complete_preds",
                        lambda model, universe, n, t, c: ([*liquid, *illiquid], "ok"))
    lines: list[str] = []
    mx.g0_section(lines, {}, "ckpt", [])
    assert any("표본 40)" in ln for ln in lines) and not any("표본 65)" in ln for ln in lines)


# -- Codex 42차 리뷰 반영 ---------------------------------------------------------------


def test_new_top_ticker_with_truncated_prefix_is_rejected(
    fetch_env: _FakeFdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pandas as pd

    newcomer = "700000"
    fetch_env.bars[newcomer] = (date(2015, 1, 2), date(2016, 3, 31))  # 2005 상장인데 2015 부터
    real_listing = fetch_env.StockListing

    def listing(name: str) -> Any:
        if name == "KOSPI":
            codes = [newcomer, *TOP[:-1]]
            return pd.DataFrame([{"Code": c, "Marcap": 1e12 - i} for i, c in enumerate(codes)])
        return real_listing(name)

    monkeypatch.setattr(fetch_env, "StockListing", listing)
    u = _run_fetch(fetch_env, monkeypatch)
    assert u["fetch_complete"] is False and newcomer in u["required_failed"]


def test_late_start_uses_listing_date_for_top_tickers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mx, "BARS_DIR", tmp_path)
    for code, first in [("111110", "2016-06-01"), ("222220", "2016-06-01"), ("333330", "2010-01-04")]:
        (tmp_path / f"{code}.csv").write_text(f"date,open,high,low,close,volume\n{first},1,1,1,1,1\n")
    listed = {"111110": "2016-05-30", "222220": "2008-01-02"}  # 111110 은 신규 상장(정상)
    # 222220 은 2008 상장인데 2016 부터 → 잘림, 333330 은 상장일 모름 → 검증 불가
    assert mx.late_start_codes(["111110", "222220", "333330"], listed) == ["222220", "333330"]
