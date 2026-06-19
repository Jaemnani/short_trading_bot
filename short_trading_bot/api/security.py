"""JWT auth helpers + the FastAPI auth dependency.

P10 uses a single configured user + HS256 token. Production should use hashed credentials,
short token TTLs, and HTTPS (the dashboard is internet-reachable and controls real trading).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .state import ApiState

_ALGO = "HS256"
_bearer = HTTPBearer(auto_error=False)


def create_access_token(sub: str, secret: str, *, expires_minutes: int = 720) -> str:
    payload = {"sub": sub, "exp": datetime.now(UTC) + timedelta(minutes=expires_minutes)}
    return jwt.encode(payload, secret, algorithm=_ALGO)


def decode_token(token: str, secret: str) -> str:
    data = jwt.decode(token, secret, algorithms=[_ALGO])
    return str(data["sub"])


def get_state(request: Request) -> ApiState:
    state: ApiState = request.app.state.api
    return state


def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        return decode_token(credentials.credentials, get_state(request).jwt_secret)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc
