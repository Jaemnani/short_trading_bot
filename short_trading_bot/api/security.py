"""JWT auth helpers + the FastAPI auth dependency.

P10 uses a single configured user + HS256 token. The dashboard controls real trading
(kill switch = flat-all), so insecure defaults are refused at startup unless the API is
bound to loopback only (``insecure_api_config``), and failed logins are throttled.
"""

from __future__ import annotations

import ipaddress
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .state import ApiState

_ALGO = "HS256"
_bearer = HTTPBearer(auto_error=False)

DEFAULT_JWT_SECRET = "dev-insecure-change-me"
_DEFAULT_PASSWORDS = frozenset({"", "admin", "password", "changeme"})
_MIN_SECRET_LEN = 32


def create_access_token(sub: str, secret: str, *, expires_minutes: int = 720) -> str:
    payload = {"sub": sub, "exp": datetime.now(UTC) + timedelta(minutes=expires_minutes)}
    return jwt.encode(payload, secret, algorithm=_ALGO)


def decode_token(token: str, secret: str) -> str:
    data = jwt.decode(token, secret, algorithms=[_ALGO])
    return str(data["sub"])


def insecure_api_config(secret: str, password: str) -> list[str]:
    """이대로 외부에 열면 안 되는 이유 목록 (빈 목록 = 안전).

    기본 시크릿은 소스에 공개돼 있어 누구나 토큰을 위조할 수 있고, 기본 비밀번호는
    그냥 로그인된다 — 둘 다 곧 '같은 네트워크의 누구나 전량청산 버튼을 누를 수 있음'."""
    reasons: list[str] = []
    if secret == DEFAULT_JWT_SECRET:
        reasons.append("STB_API_JWT_SECRET is the public default")
    elif len(secret) < _MIN_SECRET_LEN:
        reasons.append(f"STB_API_JWT_SECRET shorter than {_MIN_SECRET_LEN} chars")
    if password in _DEFAULT_PASSWORDS:
        reasons.append("STB_API_PASSWORD is empty or a default")
    return reasons


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def credentials_match(state: ApiState, username: str, password: str) -> bool:
    # 둘 다 항상 비교한다 (단락 평가가 사용자명 일치 여부를 시간으로 흘리지 않게).
    user_ok = secrets.compare_digest(username.encode(), state.username.encode())
    pass_ok = secrets.compare_digest(password.encode(), state.password.encode())
    return user_ok and pass_ok


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
