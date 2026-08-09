"""KakaoNotifier — 토큰 수명주기·발송 형식·폭주 가드 검증 (오프라인, transport 주입)."""

from pathlib import Path
from typing import Any

import pytest

from short_trading_bot.infra.notifier.kakao import (
    API_HOST,
    AUTH_HOST,
    KakaoNotifier,
    KakaoToken,
    apply_token_response,
    exchange_auth_code,
    extract_auth_code,
)


class FakeTransport:
    """호출 기록 + URL별 응답 스크립트."""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self._responses = responses

    async def __call__(
        self, url: str, headers: dict[str, str], data: dict[str, str]
    ) -> dict[str, Any]:
        self.calls.append((url, headers, data))
        for prefix, resp in self._responses.items():
            if url.startswith(prefix):
                return resp
        raise AssertionError(f"unexpected url: {url}")


def _token_file(tmp_path: Path, *, expires_in: float, now: float = 1000.0) -> Path:
    path = tmp_path / "kakao_token.json"
    KakaoToken("acc", "ref", access_expires_at=now + expires_in).save(path)
    return path


class TestTokenLifecycle:
    def test_needs_refresh_with_margin(self) -> None:
        token = KakaoToken("a", "r", access_expires_at=1000.0)
        assert not token.needs_refresh(now=389.9)  # 만료 610초 전 — 아직 여유
        assert token.needs_refresh(now=400.0)  # 만료 600초 전부터 미리 갱신
        assert token.needs_refresh(now=2000.0)

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        KakaoToken("acc", "ref", access_expires_at=123.0).save(path)
        loaded = KakaoToken.load(path)
        assert loaded == KakaoToken("acc", "ref", access_expires_at=123.0)

    def test_load_missing_or_corrupt(self, tmp_path: Path) -> None:
        assert KakaoToken.load(tmp_path / "none.json") is None
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert KakaoToken.load(bad) is None

    def test_refresh_response_without_new_refresh_token_keeps_old(self) -> None:
        # 카카오는 리프레시 토큰 잔여 1개월+ 이면 refresh_token 을 응답에 안 넣는다 —
        # 이때 기존 것을 비우면 다음 갱신부터 영구 실패하므로 반드시 유지.
        token = apply_token_response("old-ref", {"access_token": "new", "expires_in": 21600}, 0.0)
        assert token.refresh_token == "old-ref"
        token = apply_token_response(
            "old-ref", {"access_token": "new", "expires_in": 21600, "refresh_token": "new-ref"}, 0.0
        )
        assert token.refresh_token == "new-ref"


class TestNotify:
    async def test_send_includes_required_link_and_result_check(self, tmp_path: Path) -> None:
        # link 필드 누락은 400/-2 (2026-07 devtalk) — 항상 포함되는지 박제.
        path = _token_file(tmp_path, expires_in=99999)
        transport = FakeTransport({API_HOST: {"result_code": 0}})
        n = KakaoNotifier("key", path, transport=transport, clock=lambda: 1000.0)
        await n.notify("engine.start", mode="PAPER")
        (url, headers, data) = transport.calls[0]
        assert url.startswith(API_HOST)
        assert headers["Authorization"] == "Bearer acc"
        assert '"link"' in data["template_object"]
        assert "[STB] engine.start" in data["template_object"]

    async def test_send_failure_raises(self, tmp_path: Path) -> None:
        path = _token_file(tmp_path, expires_in=99999)
        transport = FakeTransport({API_HOST: {"result_code": -2, "msg": "bad"}})
        n = KakaoNotifier("key", path, transport=transport, clock=lambda: 1000.0)
        with pytest.raises(RuntimeError, match="kakao send failed"):
            await n.notify("x")

    async def test_expired_token_refreshed_and_persisted(self, tmp_path: Path) -> None:
        path = _token_file(tmp_path, expires_in=10, now=1000.0)  # 곧 만료 → 갱신 대상
        transport = FakeTransport(
            {
                AUTH_HOST: {"access_token": "acc2", "expires_in": 21600, "refresh_token": "ref2"},
                API_HOST: {"result_code": 0},
            }
        )
        n = KakaoNotifier("key", path, transport=transport, clock=lambda: 1000.0)
        await n.notify("x")
        assert [u for (u, _, _) in transport.calls] == [
            f"{AUTH_HOST}/oauth/token",
            f"{API_HOST}/v2/api/talk/memo/default/send",
        ]
        saved = KakaoToken.load(path)
        assert saved is not None and saved.access_token == "acc2"
        assert saved.refresh_token == "ref2"

    async def test_missing_token_file_hints_auth_command(self, tmp_path: Path) -> None:
        n = KakaoNotifier("key", tmp_path / "none.json", transport=FakeTransport({}))
        with pytest.raises(RuntimeError, match="kakao-auth"):
            await n.notify("x")

    async def test_message_truncated_to_kakao_limit(self, tmp_path: Path) -> None:
        assert len(KakaoNotifier.format("e", {"k": "x" * 500})) == 200

    async def test_throttle_caps_per_minute_then_recovers(self, tmp_path: Path) -> None:
        path = _token_file(tmp_path, expires_in=99999)
        transport = FakeTransport({API_HOST: {"result_code": 0}})
        clock = {"t": 1000.0}
        n = KakaoNotifier(
            "key", path, transport=transport, max_per_minute=3, clock=lambda: clock["t"]
        )
        for _ in range(5):
            await n.notify("burst")
        assert len(transport.calls) == 3  # 초과 2건은 조용히 드랍 (로그만)
        clock["t"] += 61  # 1분 경과 → 윈도 비워짐
        await n.notify("later")
        assert len(transport.calls) == 4


class TestExtractAuthCode:
    """사람이 붙여넣는 형태는 제각각 — 코드만, URL 전체, 따옴표/공백 포함."""

    def test_bare_code_passthrough(self) -> None:
        assert extract_auth_code("ABC123") == "ABC123"

    def test_full_redirect_url(self) -> None:
        assert extract_auth_code("http://localhost:8899/kakao?code=ABC123") == "ABC123"

    def test_url_with_extra_params(self) -> None:
        url = "http://localhost:8899/kakao?code=ABC123&state=x"
        assert extract_auth_code(url) == "ABC123"

    def test_strips_whitespace_and_quotes(self) -> None:
        assert extract_auth_code("  'ABC123'  ") == "ABC123"

    def test_query_fragment_only(self) -> None:
        assert extract_auth_code("?code=ABC123") == "ABC123"


class TestAuthCodeExchange:
    async def test_exchange_success(self) -> None:
        transport = FakeTransport(
            {AUTH_HOST: {"access_token": "a", "refresh_token": "r", "expires_in": 21600}}
        )
        token = await exchange_auth_code("key", "http://localhost:8899/kakao", "CODE", transport=transport)
        assert token.access_token == "a" and token.refresh_token == "r"
        (_, _, data) = transport.calls[0]
        assert data["grant_type"] == "authorization_code" and data["code"] == "CODE"

    async def test_exchange_error_payload_raises(self) -> None:
        transport = FakeTransport({AUTH_HOST: {"error": "invalid_grant"}})
        with pytest.raises(RuntimeError, match="kakao auth failed"):
            await exchange_auth_code("key", "uri", "BAD", transport=transport)
