"""Remote control state (RUNNING / PAUSED / STOPPED).

- PAUSE blocks NEW entries; open positions keep being managed/stopped.
- STOP (kill switch) blocks new entries AND requests a flat-all liquidation.

In P5 this is in-memory; P10 backs it with a DB/Redis flag so any device can toggle it
and the running engine observes the change. Every change should be audit-logged by the caller.
"""

from __future__ import annotations

from ..domain.enums import ControlState


class ControlSwitch:
    def __init__(self, state: ControlState = ControlState.RUNNING) -> None:
        self._state = state
        self._flat_all_requested = False
        self._scope: str | None = None  # None = global; else campaign_id

    @property
    def state(self) -> ControlState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state is ControlState.RUNNING

    @property
    def is_paused(self) -> bool:
        return self._state is ControlState.PAUSED

    @property
    def is_stopped(self) -> bool:
        return self._state is ControlState.STOPPED

    @property
    def flat_all_requested(self) -> bool:
        return self._flat_all_requested

    @property
    def scope(self) -> str | None:
        return self._scope

    def pause(self) -> None:
        self._state = ControlState.PAUSED

    def resume(self) -> None:
        self._state = ControlState.RUNNING
        self._flat_all_requested = False
        self._scope = None

    def stop(self, scope: str | None = None) -> None:
        """Kill switch: halt new entries and request flat-all (optionally campaign-scoped)."""
        self._state = ControlState.STOPPED
        self._flat_all_requested = True
        self._scope = scope

    def clear_flat_all(self) -> None:
        """Called once the engine has issued the liquidation orders."""
        self._flat_all_requested = False
