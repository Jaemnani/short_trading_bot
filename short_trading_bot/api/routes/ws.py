from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..state import ApiState

router = APIRouter(tags=["ws"])


@router.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """Live channel. On connect pushes the control state; the engine publishes
    position/PnL/signal/news updates here in deployment. (Auth via query token in prod.)"""
    await websocket.accept()
    state: ApiState = websocket.app.state.api
    await websocket.send_json({"type": "control", "state": state.control.state.value})
    try:
        while True:
            message = await websocket.receive_text()
            await websocket.send_json({"type": "ack", "echo": message})
    except WebSocketDisconnect:
        return
