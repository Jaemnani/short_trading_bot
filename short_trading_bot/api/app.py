"""FastAPI app factory. Mounts auth + dashboard routes and a live WebSocket.

CORS is wide-open for local PWA dev; lock ``allow_origins`` to the dashboard origin and
serve behind HTTPS in production (this endpoint controls real trading)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .routes import auth, control, positions, status, strategies, ws
from .state import ApiState

_FRONTEND_DIST = Path("frontend/dist")


def create_app(state: ApiState) -> FastAPI:
    app = FastAPI(title="short_trading_bot API", version="0.1.0")
    app.state.api = state
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # dev only
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth.router)
    app.include_router(strategies.router)
    app.include_router(control.router)
    app.include_router(positions.router)
    app.include_router(status.router)
    app.include_router(ws.router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok", "control": state.control.state.value}

    # 빌드된 대시보드(frontend/dist)를 같은 포트에서 서빙 — http://<호스트>:8000 이 곧 현황 페이지.
    # 라우트 등록 뒤의 catch-all 마운트라 /api/* /health /ws 는 영향 없음. dist 없으면 API 전용.
    if _FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="dashboard")

    return app
