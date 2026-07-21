"""Load the trading config (watchlist + risk limits) from a JSON file.

This makes the bot configurable per operator without code changes — point `trader serve`
at a JSON file. (A per-user settings UI replaces this file source later; the engine reads
a watchlist + RiskLimits regardless of source.)

Format:
{
  "limits": {"daily_loss_limit": "500000", "max_open_positions": 5,
             "max_order_notional": "5000000", "max_ticker_exposure": "10000000"},
  "watchlist": {
    "005930": {"strategy_id": "trend_long_v1", "market": "KRX", "resolution": "1D",
               "risk_per_trade": 0.01, "strategy_params": {"require_confirm": false}}
  }
}
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..domain.params import StopConfig
from ..risk.limits import RiskLimits
from ..strategy.templates import StrategyTemplate


class ScannerConfig(BaseModel):
    """장중 종목검색(스캐너) 설정 — config JSON의 선택적 ``scanner`` 섹션.

    거래량순위 API를 ``interval_seconds`` 주기로 폴링해 급등+거래량 급증 종목을
    자동 합류시킨다. ``favorites``(며칠 일봉 스캔 후보군)는 완화된 문턱 적용.
    """

    enabled: bool = False
    interval_seconds: float = Field(default=300.0, ge=30.0)  # 장중 스캔 주기
    max_active: int = Field(default=3, ge=1, le=10)  # 하루 스캐너 합류 종목 상한
    min_change_pct: float = 3.0  # 등락률 하한 (%)
    max_change_pct: float | None = 15.0  # 등락률 상한 (%) — 과열 추격 방지, null=무제한
    min_vol_surge: float = 150.0  # 거래량증가율 하한 (%)
    min_value_traded: float = 5_000_000_000  # 누적 거래대금 하한 (원)
    rejoin: bool = False  # False = 랏 1회전 후 그 종목 재진입 금지 (churn 방지)
    regime_filter: bool = False  # True = 시장 레짐 나쁠 때(코스피 20일선 아래/당일 급락) 합류·진입 중단
    # DART 위험공시 필터 (기본 꺼짐 — 6.5개월 사후검증: 위험공시 거래 5/174건뿐, 합계 +3.5만이라
    # 켰다면 오히려 손해. 호재성 거래정지 오탐도 있음. 표본 축적 후 재검증 예정)
    dart_filter: bool = False
    record_rankings: bool = True  # 순위 응답을 data/rankings/에 저장 (사후 검증용)
    favorite_relax: float = Field(default=0.7, gt=0, le=1.0)  # 후보군 문턱 완화 배율
    daily_candidates: int = Field(default=20, ge=0)  # 일봉 스캔 후보군 크기 (0=끔)
    daily_universe: int = Field(default=200, ge=10)  # 일봉 스캔 대상 시총 상위 N
    resolution: str = "5m"
    risk_per_trade: float = Field(default=0.005, gt=0, le=1.0)
    sizing_cost_buffer_pct: float = Field(default=0.0, ge=0, le=0.02)  # 손절 실손실 예산 보정
    chandelier_mult: float = Field(default=3.0, gt=0)  # 트레일링 스톱 배수 (lot-level)
    strategy_id: str = "momo_intraday_v1"
    strategy_params: dict[str, Any] = Field(default_factory=dict)

    def template(self) -> StrategyTemplate:
        return StrategyTemplate(
            strategy_id=self.strategy_id,
            resolution=self.resolution,  # type: ignore[arg-type]
            risk_per_trade=self.risk_per_trade,
            stop=StopConfig(chandelier_mult=self.chandelier_mult),
            strategy_params=self.strategy_params,
            regime_filter=self.regime_filter,
            sizing_cost_buffer_pct=self.sizing_cost_buffer_pct,
        )


def _dec(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def load_trading_config(path: str | Path) -> tuple[dict[str, StrategyTemplate], RiskLimits]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    watchlist = {
        ticker: StrategyTemplate(**cfg) for ticker, cfg in data.get("watchlist", {}).items()
    }
    lim = data.get("limits", {})
    limits = RiskLimits(
        max_open_positions=lim.get("max_open_positions"),
        max_order_notional=_dec(lim.get("max_order_notional")),
        max_ticker_exposure=_dec(lim.get("max_ticker_exposure")),
        daily_loss_limit=_dec(lim.get("daily_loss_limit")),
        daily_loss_pct=lim.get("daily_loss_pct"),  # 권장: 자본 대비 비율 (예: 0.03)
        max_drawdown_pct=lim.get("max_drawdown_pct"),  # 총 낙폭 브레이크 (예: 0.15)
    )
    return watchlist, limits


def load_scanner_config(path: str | Path) -> ScannerConfig:
    """``scanner`` 섹션 파싱 (없으면 disabled 기본값)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return ScannerConfig(**data.get("scanner", {}))


def load_paper_cash(path: str | Path) -> Decimal | None:
    """시뮬레이션 실행(기본 serve)의 페이퍼 자본금 — 실제 모의투자 계좌 잔고와
    맞춰야 사이징·포워드 결과가 실계좌와 일치한다. 없으면 브로커 기본값(1억)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return _dec(data.get("paper_cash"))
