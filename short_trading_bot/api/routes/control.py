from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

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
    if body.action == "stop" and body.scope:
        # 엔진은 캠페인 단위 청산을 지원하지 않는다 (파일 브리지는 action 만 전달 → 전역
        # flat-all). 부분 청산을 기대한 요청이 조용히 전량청산으로 커지지 않게 거부한다.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "scoped stop is not supported by the engine"
        )
    control = get_state(request).control
    if body.action == "pause":
        control.pause()
    elif body.action == "resume":
        control.resume()
    elif body.action == "stop":  # 긴급중지 (kill switch -> flat-all)
        control.stop()
    if body.action in ("pause", "resume", "stop"):
        # 엔진은 별도 프로세스 — 파일 브리지로 전달해야 실제로 멈춘다 (엔진이 2초마다 읽음).
        from ...risk.control_file import mark_kill_switch, write_command

        state = get_state(request)
        if body.action == "stop":
            # 긴급중지 표시를 명령보다 먼저 남긴다. 엔진이 꺼져 있는 동안 누른 stop 은
            # control.json 만으로는 전달되지 않는다(엔진은 시작 시 잔존 명령을 과거 것으로
            # 본다) — 이 표시가 있으면 엔진은 STOPPED+전량청산으로 시작한다.
            mark_kill_switch(state.kill_switch_file)
        write_command(body.action, path=state.control_file)
        if body.action == "resume":
            # resume 명령을 먼저 영속한 뒤에 긴급중지 표시를 지운다. 순서가 반대면 그 사이에
            # 죽었을 때 표시는 없고 control.json 엔 옛 stop 만 남아(엔진은 시작 시 과거 명령으로
            # 무시) 재기동한 엔진이 전달된 적 없는 resume 상태로 매매를 재개한다.
            state.kill_switch_file.unlink(missing_ok=True)
    return _out(request)
