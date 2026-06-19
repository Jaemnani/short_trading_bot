from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..schemas import ControlIn, ControlOut
from ..security import get_state, require_auth

router = APIRouter(prefix="/api", tags=["control"])


def _out(request: Request) -> ControlOut:
    control = get_state(request).control
    return ControlOut(
        state=control.state.value,
        flat_all_requested=control.flat_all_requested,
        scope=control.scope,
    )


@router.get("/control", response_model=ControlOut)
def get_control(request: Request, _user: str = Depends(require_auth)) -> ControlOut:
    return _out(request)


@router.post("/control", response_model=ControlOut)
def set_control(body: ControlIn, request: Request, _user: str = Depends(require_auth)) -> ControlOut:
    control = get_state(request).control
    if body.action == "pause":
        control.pause()
    elif body.action == "resume":
        control.resume()
    elif body.action == "stop":  # 긴급중지 (kill switch -> flat-all)
        control.stop(scope=body.scope)
    return _out(request)
