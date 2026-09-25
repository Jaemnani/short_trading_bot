from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from ..schemas import LoginIn, TokenOut
from ..security import create_access_token, credentials_match, get_state

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/auth/login", response_model=TokenOut)
def login(body: LoginIn, request: Request) -> TokenOut:
    state = get_state(request)
    client = request.client.host if request.client is not None else "unknown"
    if state.login_throttle.blocked(client):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many failed logins")
    if not credentials_match(state, body.username, body.password):
        state.login_throttle.record_failure(client)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    state.login_throttle.reset(client)
    return TokenOut(access_token=create_access_token(body.username, state.jwt_secret))
