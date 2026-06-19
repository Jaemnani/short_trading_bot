from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from ..schemas import LoginIn, TokenOut
from ..security import create_access_token, get_state

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/auth/login", response_model=TokenOut)
def login(body: LoginIn, request: Request) -> TokenOut:
    state = get_state(request)
    if body.username != state.username or body.password != state.password:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    return TokenOut(access_token=create_access_token(body.username, state.jwt_secret))
