from datetime import datetime

from short_trading_bot.app.scheduler import Scheduler
from short_trading_bot.infra.notifier.base import (
    CompositeNotifier,
    ConsoleNotifier,
    InMemoryNotifier,
    Notifier,
)
from short_trading_bot.risk.rate_limit import TokenBucket


class _Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# --- TokenBucket ---

def test_token_bucket_drains_and_refills() -> None:
    clock = _Clock(0.0)
    bucket = TokenBucket(rate_per_sec=2, capacity=2, clock=clock)
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    assert not bucket.try_acquire()  # empty
    clock.t = 0.5  # +0.5s * 2/s = 1 token
    assert bucket.try_acquire()
    assert not bucket.try_acquire()


def test_token_bucket_caps_at_capacity() -> None:
    clock = _Clock(0.0)
    bucket = TokenBucket(rate_per_sec=5, capacity=5, clock=clock)
    bucket.try_acquire(5)
    clock.t = 100.0  # huge elapsed must not exceed capacity
    assert bucket.available == 5.0


# --- Notifier ---

async def test_inmemory_notifier_records() -> None:
    n = InMemoryNotifier()
    await n.notify("order.filled", lot="lot1", qty=10)
    assert n.sent[0].event == "order.filled"
    assert n.sent[0].fields == {"lot": "lot1", "qty": 10}


async def test_composite_fans_out_and_isolates_failures() -> None:
    class _Failing(Notifier):
        async def notify(self, event: str, **fields: object) -> None:
            raise RuntimeError("channel down")

    a, b = InMemoryNotifier(), InMemoryNotifier()
    composite = CompositeNotifier(a, _Failing(), b)
    await composite.notify("kill_switch")  # must not raise despite the failing backend
    assert a.sent and b.sent


async def test_console_notifier_runs() -> None:
    await ConsoleNotifier().notify("eod.report", pnl=123)


# --- Scheduler ---

async def test_scheduler_fires_once_per_day() -> None:
    calls: list[int] = []

    async def job() -> None:
        calls.append(1)

    s = Scheduler()
    s.add_daily("eod", 15, 30, job)

    assert await s.run_pending(datetime(2026, 1, 2, 15, 0)) == []  # before time
    assert await s.run_pending(datetime(2026, 1, 2, 15, 30)) == ["eod"]
    assert await s.run_pending(datetime(2026, 1, 2, 16, 0)) == []  # already ran today
    assert await s.run_pending(datetime(2026, 1, 3, 15, 30)) == ["eod"]  # next day
    assert len(calls) == 2
