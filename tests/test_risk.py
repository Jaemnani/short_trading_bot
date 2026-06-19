from decimal import Decimal

from short_trading_bot.domain.enums import ControlState, PositionState
from short_trading_bot.domain.factory import PositionFactory
from short_trading_bot.domain.signal import IntentKind, Signal
from short_trading_bot.risk.control import ControlSwitch
from short_trading_bot.risk.kill_switch import build_flat_all_intents
from short_trading_bot.risk.limits import RiskLimits, RiskSnapshot
from short_trading_bot.risk.manager import RiskManager
from short_trading_bot.strategy.templates import StrategyTemplate


def _snap(**over) -> RiskSnapshot:
    base = dict(equity=Decimal("10000000"), open_positions=0, daily_pnl=Decimal("0"))
    base.update(over)
    return RiskSnapshot(**base)  # type: ignore[arg-type]


def _check(rm: RiskManager, kind: IntentKind, *, notional="0", ticker="005930", snap=None):
    return rm.check(
        intent_kind=kind,
        ticker=ticker,
        order_notional=Decimal(notional),
        snapshot=snap or _snap(),
    )


# --- RiskManager gate ---

def test_exits_always_allowed_even_when_stopped() -> None:
    ctrl = ControlSwitch(ControlState.STOPPED)
    rm = RiskManager(RiskLimits(), ctrl)
    assert _check(rm, IntentKind.EXIT).allowed
    assert _check(rm, IntentKind.TRIM).allowed


def test_entry_blocked_when_paused_and_stopped() -> None:
    rm = RiskManager(RiskLimits(), ControlSwitch(ControlState.PAUSED))
    assert _check(rm, IntentKind.ENTER).reason == "paused"
    rm2 = RiskManager(RiskLimits(), ControlSwitch(ControlState.STOPPED))
    assert _check(rm2, IntentKind.ENTER).reason == "stopped"


def test_limit_breaches_block_entry() -> None:
    limits = RiskLimits(
        max_open_positions=3,
        max_order_notional=Decimal("1000000"),
        max_ticker_exposure=Decimal("2000000"),
        daily_loss_limit=Decimal("500000"),
    )
    rm = RiskManager(limits)
    assert _check(rm, IntentKind.ENTER, snap=_snap(daily_pnl=Decimal("-500000"))).reason == "daily_loss_limit"
    assert _check(rm, IntentKind.ENTER, snap=_snap(open_positions=3)).reason == "max_open_positions"
    assert _check(rm, IntentKind.ENTER, notional="2000000").reason == "order_notional"
    # within order_notional (800k <= 1M) but projected ticker exposure 1.5M + 0.8M > 2M
    assert (
        _check(rm, IntentKind.ENTER, notional="800000", snap=_snap(ticker_exposure={"005930": Decimal("1500000")})).reason
        == "ticker_exposure"
    )


def test_entry_allowed_within_limits() -> None:
    rm = RiskManager(RiskLimits(max_open_positions=5, max_order_notional=Decimal("5000000")))
    assert _check(rm, IntentKind.ENTER, notional="1000000").allowed


# --- ControlSwitch ---

def test_control_transitions() -> None:
    c = ControlSwitch()
    assert c.is_running
    c.pause()
    assert c.is_paused
    c.stop(scope="campaign-1")
    assert c.is_stopped and c.flat_all_requested and c.scope == "campaign-1"
    c.clear_flat_all()
    assert not c.flat_all_requested
    c.resume()
    assert c.is_running and c.scope is None


# --- kill switch flat-all ---

def _lot(state: PositionState):
    lot = PositionFactory.create(Signal(ticker="005930"), StrategyTemplate(strategy_id="trend_long_v1"))
    lot.state = state
    lot.qty = Decimal("10")
    return lot


def test_flat_all_targets_only_open_lots() -> None:
    lots = [_lot(PositionState.HOLDING), _lot(PositionState.WATCHING), _lot(PositionState.CLOSED)]
    out = build_flat_all_intents(lots)
    assert len(out) == 1
    lot, intent = out[0]
    assert lot.state is PositionState.HOLDING
    assert intent.kind is IntentKind.EXIT and intent.reason == "kill_switch"
