"""FastAPI app factory. Mounts auth + dashboard routes and a live WebSocket.

CORS is wide-open for local PWA dev; lock ``allow_origins`` to the dashboard origin and
serve behind HTTPS in production (this endpoint controls real trading)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import auth, control, positions, strategies, ws
from .state import ApiState


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
    app.include_router(ws.router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok", "control": state.control.state.value}

    return app
