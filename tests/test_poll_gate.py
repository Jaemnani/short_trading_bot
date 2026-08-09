"""PollGate — 세션 창 경계·백오프 진행·리셋 검증.

근거가 된 사고: 2026-08-07~08 fill_poll.error 699건이 전부 장외(금 20:00 이후~주말)에
발생, 장중 0건. 게이트가 그 관측을 정확히 재현하는지(장외=쉼, 장중=폴링) 박제한다.
"""

from datetime import datetime

from short_trading_bot.execution.poll_gate import KST, PollGate


def _kst(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=KST)


# 2026-08-07 = 금요일, 08-08 = 토요일, 08-10 = 월요일
FRI = (2026, 8, 7)
SAT = (2026, 8, 8)
MON = (2026, 8, 10)


class TestSessionWindow:
    def test_weekday_inside_window(self) -> None:
        gate = PollGate()
        assert gate.in_session(_kst(*FRI, 8, 30))  # 창 시작 경계 포함
        assert gate.in_session(_kst(*FRI, 12, 0))
        assert gate.in_session(_kst(*FRI, 16, 30))  # 창 끝 경계 포함

    def test_weekday_outside_window(self) -> None:
        gate = PollGate()
        assert not gate.in_session(_kst(*FRI, 8, 29))
        assert not gate.in_session(_kst(*FRI, 16, 31))
        assert not gate.in_session(_kst(*FRI, 20, 0))  # 사고 당시 에러 시작 시각
        assert not gate.in_session(_kst(*FRI, 23, 59))

    def test_weekend_always_idle(self) -> None:
        gate = PollGate()
        assert not gate.in_session(_kst(*SAT, 10, 0))  # 사고 당시 토요일 오전
        assert not gate.in_session(_kst(2026, 8, 9, 12, 0))  # 일요일

    def test_monday_resumes(self) -> None:
        gate = PollGate()
        assert gate.in_session(_kst(*MON, 9, 0))

    def test_utc_input_is_converted(self) -> None:
        # 호출부가 UTC 를 넘겨도 KST 로 환산해 판정해야 한다 (00:00Z = 같은 날 09:00 KST).
        from datetime import UTC

        gate = PollGate()
        assert not gate.in_session(datetime(2026, 8, 8, 0, 0, tzinfo=UTC))  # 토 09:00 KST
        assert gate.in_session(datetime(2026, 8, 7, 0, 0, tzinfo=UTC))  # 금 09:00 KST


class TestHolidays:
    """공휴일 평일은 장이 열리지 않는다 — 표에 있으면 쉬고, 없는 연도는 평일 취급(백오프가 완화)."""

    def test_known_holiday_weekday_is_idle(self) -> None:
        gate = PollGate()
        assert not gate.in_session(_kst(2026, 10, 9, 10, 0))  # 한글날 (금)
        assert not gate.in_session(_kst(2026, 5, 5, 10, 0))  # 어린이날 (화)

    def test_day_before_and_after_holiday_is_session(self) -> None:
        gate = PollGate()
        assert gate.in_session(_kst(2026, 10, 8, 10, 0))  # 목
        assert gate.in_session(_kst(2026, 10, 12, 10, 0))  # 월

    def test_holiday_outside_window_still_idle(self) -> None:
        gate = PollGate()
        assert not gate.in_session(_kst(2026, 10, 9, 3, 0))

    def test_unknown_year_degrades_to_weekday(self) -> None:
        # 표를 갱신 안 한 미래 연도는 평일로 취급 — 안전한 열화(백오프가 60s 로 누름).
        gate = PollGate()
        assert gate.in_session(_kst(2030, 1, 1, 10, 0))  # 화요일


class TestBackoff:
    def test_no_failures_uses_base(self) -> None:
        gate = PollGate(base_seconds=2.0)
        assert gate.next_delay(_kst(*FRI, 10, 0)) == 2.0

    def test_backoff_doubles_and_caps(self) -> None:
        gate = PollGate(base_seconds=2.0, max_backoff_seconds=60.0)
        now = _kst(*FRI, 10, 0)
        expected = [4.0, 8.0, 16.0, 32.0, 60.0, 60.0]  # 2*2^n, 상한 60
        for want in expected:
            gate.record(ok=False)
            assert gate.next_delay(now) == want

    def test_success_resets_backoff(self) -> None:
        gate = PollGate(base_seconds=2.0)
        now = _kst(*FRI, 10, 0)
        for _ in range(4):
            gate.record(ok=False)
        assert gate.next_delay(now) > 2.0
        gate.record(ok=True)
        assert gate.next_delay(now) == 2.0
        assert gate.consecutive_failures == 0

    def test_idle_delay_outside_session(self) -> None:
        # 장외에는 실패 여부와 무관하게 idle 간격 (백오프 상태를 오염시키지 않음).
        gate = PollGate(base_seconds=2.0, idle_seconds=60.0)
        assert gate.next_delay(_kst(*SAT, 10, 0)) == 60.0
        gate.record(ok=False)
        assert gate.next_delay(_kst(*SAT, 10, 0)) == 60.0
