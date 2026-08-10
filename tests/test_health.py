"""EngineHealth — '살아는 있는데 일을 하나' 판정 검증.

핵심 불변식: 장외 무소식은 정상(경고 없음), 장중 무소식은 이상.
이 구분이 없으면 주말마다 거짓 경보가 울려 알림 자체를 무시하게 된다.
"""

from datetime import UTC, datetime, timedelta

from short_trading_bot.app.health import STALE_FEED_SECONDS, EngineHealth
from short_trading_bot.execution.poll_gate import KST


def _kst(hh: int, mm: int, *, day: int = 7) -> datetime:
    # 2026-08-07 = 금요일, 08-08 = 토요일
    return datetime(2026, 8, day, hh, mm, tzinfo=KST)


class TestFeedHealth:
    def test_no_bars_outside_session_is_ok(self) -> None:
        # 주말·야간에 봉이 없는 건 정상 — 여기서 경고하면 알림 신뢰가 무너진다.
        h = EngineHealth()
        assert h.feed_ok(_kst(10, 0, day=8))  # 토요일
        assert h.feed_ok(_kst(23, 0))  # 금요일 밤

    def test_no_bars_during_session_is_not_ok(self) -> None:
        h = EngineHealth()
        assert not h.feed_ok(_kst(10, 0))  # 장중인데 한 번도 못 받음

    def test_fresh_bar_during_session_is_ok(self) -> None:
        h = EngineHealth()
        h.on_bar("005930", _kst(10, 0))
        assert h.feed_ok(_kst(10, 1))

    def test_stale_bar_during_session_is_not_ok(self) -> None:
        h = EngineHealth()
        h.on_bar("005930", _kst(10, 0))
        just_over = _kst(10, 0) + timedelta(seconds=STALE_FEED_SECONDS + 1)
        assert not h.feed_ok(just_over)

    def test_boundary_exactly_at_threshold_is_ok(self) -> None:
        h = EngineHealth()
        h.on_bar("005930", _kst(10, 0))
        assert h.feed_ok(_kst(10, 0) + timedelta(seconds=STALE_FEED_SECONDS))

    def test_stale_bar_outside_session_is_ok(self) -> None:
        # 15:30 마지막 봉 → 밤 10시에도 경고하면 안 된다.
        h = EngineHealth()
        h.on_bar("005930", _kst(15, 30))
        assert h.feed_ok(_kst(22, 0))

    def test_utc_input_normalized_to_kst(self) -> None:
        h = EngineHealth()
        h.on_bar("005930", datetime(2026, 8, 7, 1, 0, tzinfo=UTC))  # = 금 10:00 KST
        assert h.last_bar_at is not None
        assert h.last_bar_at.hour == 10
        assert h.feed_ok(_kst(10, 1))


class TestPollHealth:
    def test_success_records_time_and_clears_failures(self) -> None:
        h = EngineHealth()
        h.on_poll(ok=False, now=_kst(10, 0))
        h.on_poll(ok=False, now=_kst(10, 1))
        assert h.poll_failures == 2
        h.on_poll(ok=True, now=_kst(10, 2))
        assert h.poll_failures == 0
        assert h.last_poll_ok_at is not None

    def test_failures_accumulate(self) -> None:
        h = EngineHealth()
        for _ in range(3):
            h.on_poll(ok=False, now=_kst(10, 0))
        assert h.poll_failures == 3
        assert h.last_poll_ok_at is None


class TestProcessErrors:
    """봉 처리 실패는 시세 연결을 끊지 않고 격리하되, 조용히 삼키지 않고 누적 노출한다."""

    def test_counter_accumulates_and_surfaces(self) -> None:
        h = EngineHealth()
        assert h.snapshot(_kst(10, 0))["process_errors"] == 0
        h.on_process_error()
        h.on_process_error()
        assert h.snapshot(_kst(10, 0))["process_errors"] == 2


class TestSnapshot:
    def test_snapshot_shape_and_values(self) -> None:
        h = EngineHealth()
        h.on_bar("005930", _kst(10, 0))
        h.on_poll(ok=True, now=_kst(10, 0))
        h.on_feed_connect()
        snap = h.snapshot(_kst(10, 2))
        assert snap["in_session"] is True
        assert snap["feed_ok"] is True
        assert snap["last_bar_ticker"] == "005930"
        assert snap["feed_stale_seconds"] == 120.0
        assert snap["bars_received"] == 1
        assert snap["feed_connects"] == 1
        assert snap["poll_failures"] == 0

    def test_snapshot_is_json_serializable(self) -> None:
        # 상태 파일(engine_status.json)에 그대로 쓰이므로 직렬화가 깨지면 안 된다.
        import json

        h = EngineHealth()
        h.on_bar("005930", _kst(10, 0))
        json.dumps(h.snapshot(_kst(10, 1)))

    def test_snapshot_empty_engine(self) -> None:
        snap = EngineHealth().snapshot(_kst(10, 0, day=8))
        assert snap["last_bar_at"] is None
        assert snap["feed_stale_seconds"] is None
        assert snap["feed_ok"] is True  # 장외라 정상
