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
    state = ApiState(
        control=control, session_factory=sf, jwt_secret=secret,
        # 브리지 파일은 반드시 tmp로 — 기본 경로를 쓰면 테스트가 실제 가동 중인
        # 엔진(data/control.json)에 stop 명령을 흘려보낸다.
        control_file=tmp_path / "control.json",
        status_file=tmp_path / "engine_status.json",
    )
    return state, sf, control


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


def test_control_post_writes_bridge_file(tmp_path) -> None:
    """대시보드 제어가 파일 브리지에 기록돼야 별도 프로세스인 엔진에 닿는다."""
    from short_trading_bot.risk.control_file import read_command

    state, _, _ = _state(tmp_path)
    client = TestClient(create_app(state))
    token = _token(client)
    client.post("/api/control", json={"action": "pause"}, headers=_auth(token))
    assert read_command(path=state.control_file) == (1, "pause")
    client.post("/api/control", json={"action": "stop"}, headers=_auth(token))
    assert read_command(path=state.control_file) == (2, "stop")  # seq 단조 증가


def test_status_alive_and_dead(tmp_path) -> None:
    """엔진 상태 파일 최신=가동, 오래됨/없음=끊김. 오늘 체결도 함께 반환."""
    import json as _json
    from datetime import UTC, datetime, timedelta

    from short_trading_bot.persistence.models import Fill, Order

    state, sf, _ = _state(tmp_path)
    client = TestClient(create_app(state))
    token = _token(client)

    r = client.get("/api/status", headers=_auth(token)).json()  # 파일 없음
    assert r["engine_alive"] is False and r["engine"] is None

    snap = {"ts": datetime.now(UTC).isoformat(), "control": "RUNNING", "equity": "10000000",
            "peak_equity": "10000000", "daily_realized": "0", "daily_date": None,
            "open_lots": [], "watching": []}
    state.status_file.write_text(_json.dumps(snap))

    async def seed() -> None:
        async with session_scope(sf) as s:
            s.add(Position(lot_id="lot1", ticker="005930", state="HOLDING",
                           strategy_id="momo_intraday_v1"))
            s.add(Order(order_id="o1", lot_id="lot1", client_order_id="c1", side="BUY",
                        qty=Decimal("10"), state="FILLED"))
            s.add(Fill(fill_id="f1", order_id="o1", lot_id="lot1", qty=Decimal("10"),
                       price=Decimal("70000")))

    asyncio.run(seed())
    r = client.get("/api/status", headers=_auth(token)).json()
    assert r["engine_alive"] is True and r["engine"]["equity"] == "10000000"
    assert r["today_fills"][0]["ticker"] == "005930" and r["today_fills"][0]["side"] == "BUY"

    snap["ts"] = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()  # 60초 전 = 끊김
    state.status_file.write_text(_json.dumps(snap))
    assert client.get("/api/status", headers=_auth(token)).json()["engine_alive"] is False


def test_ws_pushes_control_state(tmp_path) -> None:
    state, _, _ = _state(tmp_path)
    client = TestClient(create_app(state))
    with client.websocket_connect("/ws") as conn:
        msg = conn.receive_json()
        assert msg["type"] == "control" and msg["state"] == "RUNNING"


def test_dashboard_index_is_not_cached(tmp_path) -> None:
    """index.html 캐시로 인한 '대시보드 빈 화면' 방지 (2026-08-11).

    index.html 은 이름이 고정이라 캐시되면 재빌드 후 사라진 자산 해시를 참조해 404 →
    화면이 통째로 비고, 사용자에겐 '8000 포트가 안 열린다'로 보인다."""
    from pathlib import Path

    if not Path("frontend/dist/index.html").is_file():
        return  # dist 미빌드 환경에서는 검증 대상 없음
    state, _, _ = _state(tmp_path)
    client = TestClient(create_app(state))
    assert "no-cache" in client.get("/").headers.get("cache-control", "")


def test_dashboard_hashed_assets_are_immutable(tmp_path) -> None:
    """해시 자산은 내용이 바뀌면 이름도 바뀌므로 영구 캐시가 안전하다."""
    from pathlib import Path

    assets = Path("frontend/dist/assets")
    if not assets.is_dir():
        return
    name = next((p.name for p in assets.iterdir()), None)
    if name is None:
        return
    state, _, _ = _state(tmp_path)
    client = TestClient(create_app(state))
    resp = client.get(f"/assets/{name}")
    assert resp.status_code == 200
    assert "immutable" in resp.headers.get("cache-control", "")
