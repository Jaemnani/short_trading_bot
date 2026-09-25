from __future__ import annotations

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from ..security import decode_token
from ..state import ApiState

router = APIRouter(tags=["ws"])


@router.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """Live channel (``/ws?token=<JWT>``). On connect pushes the control state; the engine
    publishes position/PnL/signal/news updates here in deployment.

    브라우저 WebSocket 은 Authorization 헤더를 못 실어 쿼리 토큰으로 인증한다. 무인증이면
    계좌 현황이 그대로 새므로 accept 전에 거부한다."""
    state: ApiState = websocket.app.state.api
    token = websocket.query_params.get("token", "")
    try:
        decode_token(token, state.jwt_secret)
    except jwt.PyJWTError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    await websocket.send_json({"type": "control", "state": state.control.state.value})
    try:
        while True:
            await websocket.receive_text()  # 클라이언트 메시지는 무시 (keep-alive 용)
    except WebSocketDisconnect:
        return
