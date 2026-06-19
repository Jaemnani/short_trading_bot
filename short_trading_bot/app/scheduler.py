"""Lightweight daily-time job scheduler.

Deployment can back this with APScheduler; the logic here (when a job is due, once-per-day)
is pure and unit-testable. Session jobs (market open/close, square-off-before-close, EOD
reconcile+report, daily-limit reset, campaign start/end) register here as async callables.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime

JobFn = Callable[[], Awaitable[None]]


@dataclass
class _Job:
    name: str
    hour: int
    minute: int
    fn: JobFn
    last_run: date | None = None


class Scheduler:
    def __init__(self) -> None:
        self._jobs: list[_Job] = []

    def add_daily(self, name: str, hour: int, minute: int, fn: JobFn) -> None:
        self._jobs.append(_Job(name=name, hour=hour, minute=minute, fn=fn))

    @property
    def job_names(self) -> list[str]:
        return [j.name for j in self._jobs]

    async def run_pending(self, now: datetime) -> list[str]:
        """Run any jobs whose daily time has passed and that haven't run today. Returns fired names."""
        fired: list[str] = []
        for job in self._jobs:
            if job.last_run == now.date():
                continue
            if (now.hour, now.minute) >= (job.hour, job.minute):
                await job.fn()
                job.last_run = now.date()
                fired.append(job.name)
        return fired
