"""세션 재시작 복구 — 설계 스펙 5 / 6.8.

manifest.json과 pump_events.jsonl / sensor_samples.csv를 읽어 estop 래치,
세션 누적량, 마지막 세션 상태를 복원한다. 마지막 줄이 불완전하면
recovery_fragments.log로 옮긴 뒤 truncate한다.

⚠ 이 모듈은 순수 조회다 — 센서·펌프 실행을 절대 자동 재개하지 않는다.
SessionStore나 하드웨어 백엔드를 import하지 않는 것이 그 보장의 일부다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from gateway.api_models import PumpState, SessionState, VOLUME_DECIMALS

MANIFEST_NAME = "manifest.json"
SENSOR_CSV_NAME = "sensor_samples.csv"
PUMP_JSONL_NAME = "pump_events.jsonl"
FRAGMENTS_LOG_NAME = "recovery_fragments.log"

#: manifest.json 헤더의 컬럼 수 (gateway.session_store.SENSOR_CSV_HEADER와 일치해야 한다)
_SENSOR_CSV_COLUMNS = 11

#: 이 상태들만 "정리 완료"로 간주한다. 그 외(unknown/preparing/recording 등)는
#: 재시작 시점에 사람 또는 상위 로직의 reconciliation이 필요하다.
_TERMINAL_STATUSES = {
    SessionState.COMPLETED.value, SessionState.PARTIAL.value, SessionState.FAILED.value,
}

_ESTOP_STATE = PumpState.EMERGENCY_STOPPED.value


@dataclass(frozen=True)
class RecoveryResult:
    session_id: str
    status: str
    needs_reconciliation: bool
    estop_latched: bool
    session_cumulative_volume_ul: float
    last_pump_state: Optional[str]
    fragments_recovered: tuple[str, ...]
    errors: tuple[str, ...]


def recover_session(path: Path) -> RecoveryResult:
    """세션 디렉터리 하나를 읽어 상태를 복구한다. 센서/펌프는 재개하지 않는다."""
    manifest, manifest_error = _read_manifest(path)

    sensor_fragments = _recover_incomplete_tail(
        path / SENSOR_CSV_NAME, path / FRAGMENTS_LOG_NAME, _is_complete_csv_row,
    )
    events, pump_fragments = _read_pump_events(path)

    session_id = (manifest or {}).get("session_id") or path.name
    status = (manifest or {}).get("status") or "unknown"
    needs_reconciliation = status not in _TERMINAL_STATUSES

    last_state = events[-1]["new_state"] if events else None
    estop_latched = last_state == _ESTOP_STATE
    cumulative = sum(float(e.get("delivered_volume_ul", 0.0)) for e in events)

    errors = tuple((manifest or {}).get("errors", []))
    if manifest_error:
        errors = errors + (manifest_error,)

    return RecoveryResult(
        session_id=session_id,
        status=status,
        needs_reconciliation=needs_reconciliation,
        estop_latched=estop_latched,
        session_cumulative_volume_ul=round(cumulative, VOLUME_DECIMALS),
        last_pump_state=last_state,
        fragments_recovered=tuple(sensor_fragments + pump_fragments),
        errors=errors,
    )


# ─────────────────────────── manifest ───────────────────────────

def _read_manifest(path: Path) -> tuple[Optional[dict], Optional[str]]:
    manifest_path = path / MANIFEST_NAME
    if not manifest_path.exists():
        return None, "manifest.json missing"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8")), None
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"manifest.json unreadable: {exc}"


# ─────────────────────────── pump events ───────────────────────────

def _read_pump_events(path: Path) -> tuple[list[dict], list[str]]:
    jsonl_path = path / PUMP_JSONL_NAME
    fragments = _recover_incomplete_tail(
        jsonl_path, path / FRAGMENTS_LOG_NAME, _is_complete_json_line,
    )
    if not jsonl_path.exists():
        return [], fragments
    events = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    return events, fragments


def _is_complete_json_line(line: str) -> bool:
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    return True


def _is_complete_csv_row(line: str) -> bool:
    return len(line.split(",")) == _SENSOR_CSV_COLUMNS


# ─────────────────────────── 불완전 줄 복구 ───────────────────────────

def _split_tail_fragment(data: bytes) -> tuple[bytes, Optional[bytes]]:
    """줄바꿈 없이 끝나면 마지막 줄이 쓰다 만 것이다."""
    if not data or data.endswith(b"\n"):
        return data, None
    idx = data.rfind(b"\n")
    if idx == -1:
        return b"", data
    return data[: idx + 1], data[idx + 1:]


def _recover_incomplete_tail(
    path: Path, fragments_log: Path, validator: Callable[[str], bool],
) -> list[str]:
    """마지막 줄이 불완전하면 recovery_fragments.log로 옮기고 truncate한다."""
    if not path.exists():
        return []
    data = path.read_bytes()
    good, tail = _split_tail_fragment(data)

    fragment_text: Optional[str] = None
    if tail:
        fragment_text = tail.decode("utf-8", errors="replace")
    else:
        lines = good.splitlines(keepends=True)
        if len(lines) > 1:  # 헤더/빈 파일뿐이면 검사할 데이터가 없다
            last = lines[-1]
            text = last.decode("utf-8", errors="replace").rstrip("\n")
            if not validator(text):
                fragment_text = last.decode("utf-8", errors="replace")
                good = b"".join(lines[:-1])

    if fragment_text is None:
        return []
    _append_fragment(fragments_log, path.name, fragment_text)
    _atomic_rewrite(path, good)
    return [fragment_text]


def _append_fragment(fragments_log: Path, source_name: str, text: str) -> None:
    with open(fragments_log, "a", encoding="utf-8") as fh:
        fh.write(f"# from {source_name}\n{text}")
        if not text.endswith("\n"):
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def _atomic_rewrite(path: Path, good: bytes) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "wb") as fh:
        fh.write(good)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)
