import asyncio
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from short_trading_bot.api.app import create_app
from short_trading_bot.api.state import ApiState
from short_trading_bot.persistence.db import init_models, session_scope
from short_trading_bot.persistence.models import Position
from short_trading_bot.risk.control import ControlSwitch


def _state(tmp_path) -> tuple[ApiState, async_sessionmaker, ControlSwitch]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/api.db", poolclass=NullPool)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(init_models(engine))
    control = ControlSwitch()
    secret = "test-secret-key-please-ignore-0123456789"  # >=32 bytes
    return ApiState(control=control, session_factory=sf, jwt_secret=secret), sf, control


def _token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    return r.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health(tmp_path) -> None:
    state, _, _ = _state(tmp_path)
    client = TestClient(create_app(state))
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_strategies_requires_auth(tmp_path) -> None:
    state, _, _ = _state(tmp_path)
    client = TestClient(create_app(state))
    assert client.get("/api/strategies").status_code == 401


def test_login_then_strategies_with_param_schema(tmp_path) -> None:
    state, _, _ = _state(tmp_path)
    client = TestClient(create_app(state))
    token = _token(client)
    r = client.get("/api/strategies", headers=_auth(token))
    assert r.status_code == 200
    by_id = {s["id"]: s for s in r.json()}
    assert "trend_long_v1" in by_id
    assert "properties" in by_id["trend_long_v1"]["params_schema"]  # for the UI dynamic form


def test_bad_credentials(tmp_path) -> None:
    state, _, _ = _state(tmp_path)
    client = TestClient(create_app(state))
    assert client.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code == 401


def test_control_pause_stop_reaches_switch(tmp_path) -> None:
    state, _, control = _state(tmp_path)
    client = TestClient(create_app(state))
    token = _token(client)
    assert client.get("/api/control", headers=_auth(token)).json()["state"] == "RUNNING"
    assert client.post("/api/control", json={"action": "pause"}, headers=_auth(token)).json()["state"] == "PAUSED"
    stopped = client.post("/api/control", json={"action": "stop"}, headers=_auth(token)).json()
    assert stopped["state"] == "STOPPED" and stopped["flat_all_requested"] is True
    assert control.is_stopped  # same ControlSwitch the engine observes


def test_positions(tmp_path) -> None:
    state, sf, _ = _state(tmp_path)

    async def seed() -> None:
        async with session_scope(sf) as s:
            s.add(
                Position(
                    lot_id="lot1", ticker="005930", state="HOLDING", strategy_id="trend_long_v1",
                    qty_filled=Decimal("10"), avg_entry_price=Decimal("70000"),
                )
            )

    asyncio.run(seed())
    client = TestClient(create_app(state))
    r = client.get("/api/positions", headers=_auth(_token(client)))
    assert r.status_code == 200
    assert r.json()[0]["ticker"] == "005930"
    assert Decimal(r.json()[0]["qty_filled"]) == Decimal("10")  # Numeric scale -> "10.00000000"


def test_ws_pushes_control_state(tmp_path) -> None:
    state, _, _ = _state(tmp_path)
    client = TestClient(create_app(state))
    with client.websocket_connect("/ws") as conn:
        msg = conn.receive_json()
        assert msg["type"] == "control" and msg["state"] == "RUNNING"
