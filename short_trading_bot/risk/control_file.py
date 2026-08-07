"""파일 경유 컨트롤 브리지 — 대시보드(API 프로세스) → 엔진(serve 프로세스).

API와 엔진은 별도 프로세스라 메모리(ControlSwitch)를 공유하지 못한다. 대시보드의
일시중지/재개/긴급중지가 실제 엔진에 닿으려면 매개가 필요하다: API가 명령을
JSON 파일에 쓰고, 엔진이 주기적으로 읽어 자기 ControlSwitch에 적용한다.

``seq``(단조 증가)로 "새 명령만 1회 적용"을 보장한다 — 엔진이 킬스위치 처리 후
flat-all을 clear해도 파일에 남은 같은 명령이 재적용되지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..infra.logging import get_logger
from .control import ControlSwitch

DEFAULT_PATH = Path("data/control.json")
_ACTIONS = ("pause", "resume", "stop")

_log = get_logger("control_file")


def write_command(action: str, *, path: str | Path = DEFAULT_PATH) -> int:
    """제어 명령을 기록하고 부여된 seq를 반환한다 (API 프로세스에서 호출)."""
    if action not in _ACTIONS:
        raise ValueError(f"unknown control action: {action!r}")
    p = Path(path)
    prev = read_command(path=p)
    seq = (prev[0] if prev is not None else 0) + 1
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seq": seq, "action": action}))
    tmp.replace(p)  # 원자적 교체 — 엔진이 반쯤 쓰인 파일을 읽지 않게
    return seq


def read_command(*, path: str | Path = DEFAULT_PATH) -> tuple[int, str] | None:
    """(seq, action) — 파일 없음/손상은 None (명령 없음으로 취급)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return int(data["seq"]), str(data["action"])
    except (ValueError, KeyError, OSError):
        _log.warning("control_file.unreadable", path=str(p))
        return None


def apply_command(control: ControlSwitch, action: str) -> None:
    if action == "pause":
        control.pause()
    elif action == "resume":
        control.resume()
    elif action == "stop":
        control.stop()
