"""FastAPI app factory. Mounts auth + dashboard routes and a live WebSocket.

The dashboard is served from the same origin, so CORS is off by default. Only origins listed
in ``ApiState.cors_origins`` (``STB_API_CORS_ORIGINS``) are allowed; ``*`` is never honored —
with a wildcard any web page the user visits could drive this API (it controls real trading)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from .routes import auth, control, positions, status, strategies, ws
from .state import ApiState

_FRONTEND_DIST = Path("frontend/dist")


class _DashboardFiles(StaticFiles):
    """index.html 은 캐시 금지, 해시 자산은 영구 캐시.

    2026-08-11 사고: index.html 에 Cache-Control 이 없어 브라우저가 휴리스틱 캐싱을 했고,
    프론트를 재빌드해 자산 파일명 해시가 바뀌자 **캐시된 옛 index.html 이 사라진 파일을
    참조해 404 → 빈 화면**이 됐다. 사용자에겐 "8000 포트가 안 열린다"로 보인다.

    파일명에 해시가 있는 /assets/* 는 내용이 바뀌면 이름도 바뀌므로 영구 캐시가 안전하고,
    이름이 고정된 index.html 만 매번 확인시키면 이 부류의 사고가 원천 차단된다.
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        if path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:  # index.html, manifest, icon — 이름이 고정이라 반드시 재확인
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(state: ApiState) -> FastAPI:
    app = FastAPI(title="short_trading_bot API", version="0.1.0")
    app.state.api = state
    origins = [o for o in state.cors_origins if o and o != "*"]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,  # 인증은 Bearer 헤더 — 쿠키 자격증명 불필요
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )
    app.include_router(auth.router)
    app.include_router(strategies.router)
    app.include_router(control.router)
    app.include_router(positions.router)
    app.include_router(status.router)
    app.include_router(ws.router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        # 무인증 생존 확인용 — 제어 상태 등 내부 정보는 싣지 않는다.
        return {"status": "ok"}

    # 빌드된 대시보드(frontend/dist)를 같은 포트에서 서빙 — http://<호스트>:8000 이 곧 현황 페이지.
    # 라우트 등록 뒤의 catch-all 마운트라 /api/* /health /ws 는 영향 없음. dist 없으면 API 전용.
    if _FRONTEND_DIST.is_dir():
        app.mount("/", _DashboardFiles(directory=_FRONTEND_DIST, html=True), name="dashboard")

    return app
