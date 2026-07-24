"""세션 저장 — 설계 스펙 5 / 6.8.

디렉터리 구조: <data_root>/sessions/<session_id>/{manifest.json, sensor_samples.csv,
pump_events.jsonl}. manifest는 같은 디렉터리의 임시 파일에 flush+fsync 후
os.replace()로 원자 교체한다. 재시작 복구 로직은 gateway/recovery.py에 있다.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, IO, Optional

from gateway.api_models import (
    PUMP_MODE,
    SCHEMA_VERSION,
    PumpState,
    SensorMode,
    SensorSample,
    SessionFinishRequest,
    SessionStartRequest,
    SessionState,
    SessionUpdateRequest,
)

logger = logging.getLogger("gateway.session_store")

#: 세션 시작 최소 여유 공간 (스펙 5)
MIN_FREE_BYTES = 500 * 1024 * 1024

#: 먼저 도달하는 조건에서 flush (스펙 5)
FLUSH_RECORD_THRESHOLD = 100
FLUSH_INTERVAL_S = 1.0

#: 이 상태로 전이하는 펌프 이벤트는 즉시 fsync (안전 정지는 유실되면 안 된다)
_FSYNC_ON_PUMP_STATES = {PumpState.STOPPED.value, PumpState.EMERGENCY_STOPPED.value}

SENSOR_CSV_HEADER = [
    "schema_version", "session_id", "source_mode", "source_timestamp_ms",
    "session_elapsed_ms", "loop_count", "raw_adc", "resistance_ohm",
    "delta_r_over_r0", "temperature_c", "battery_pct",
]

MANIFEST_NAME = "manifest.json"
SENSOR_CSV_NAME = "sensor_samples.csv"
PUMP_JSONL_NAME = "pump_events.jsonl"


class SessionConflictError(Exception):
    """세션 디렉터리 충돌 또는 run ID 교체 시도 — 409에 대응."""


class InsufficientSpaceError(Exception):
    """시작 전 여유 공간이 최소 기준 미만이다."""


class SessionNotActiveError(Exception):
    """활성 세션이 없는 상태에서 세션 전용 동작을 요청했다."""


class SessionIoError(OSError):
    """세션 데이터를 기록하는 도중 발생한 I/O 오류를 감싼다."""


def _default_free_space_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


@dataclass(frozen=True)
class PumpEvent:
    """append_pump_event가 저장하는 상태 전이 1건."""

    ts_ms: int
    previous_state: str
    new_state: str
    cause: str
    request_id: str
    delivered_volume_ul: float


@dataclass(frozen=True)
class SessionSnapshot:
    """manifest.json의 불변 스냅샷 — 매 호출마다 새 객체로 반환된다."""

    schema_version: int
    session_id: str
    experiment_name: str
    started_at: str
    finished_at: Optional[str]
    status: str
    clinostat_run_id: Optional[str]
    sensor_mode: str
    pump_mode: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class _ActiveSession:
    session_id: str
    experiment_name: str
    started_at: str
    finished_at: Optional[str]
    status: str
    clinostat_run_id: Optional[str]
    sensor_mode: str
    pump_mode: str
    errors: tuple[str, ...]


def _atomic_write_json(path: Path, payload: dict) -> None:
    """같은 디렉터리의 임시 파일에 쓰고 flush+fsync 후 원자 교체한다."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


class SessionStore:
    def __init__(
        self,
        data_root: Path,
        *,
        sensor_mode: SensorMode = SensorMode.CSV_REPLAY,
        free_space_bytes: Callable[[Path], int] = _default_free_space_bytes,
        min_free_bytes: int = MIN_FREE_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._data_root = Path(data_root)
        self._sessions_root = self._data_root / "sessions"
        self._sessions_root.mkdir(parents=True, exist_ok=True)
        self._sensor_mode = sensor_mode.value
        self._free_space_bytes = free_space_bytes
        self._min_free_bytes = min_free_bytes
        self._clock = clock

        self._active: Optional[_ActiveSession] = None
        self._session_dir: Optional[Path] = None
        self._sensor_fh: Optional[IO[str]] = None
        self._pump_fh: Optional[IO[str]] = None
        self._sensor_count = 0
        self._sensor_last_flush = 0.0
        self._pump_count = 0
        self._pump_last_flush = 0.0

    # ─────────────────────────── 공개 API ───────────────────────────

    def start(self, request: SessionStartRequest) -> SessionSnapshot:
        session_dir = self._sessions_root / request.session_id
        if session_dir.exists():
            raise SessionConflictError(
                f"session directory already exists: {request.session_id}"
            )
        free = self._free_space_bytes(self._data_root)
        if free < self._min_free_bytes:
            raise InsufficientSpaceError(
                f"only {free} bytes free; minimum required is {self._min_free_bytes}"
            )

        session_dir.mkdir(parents=True)
        self._session_dir = session_dir
        self._active = _ActiveSession(
            session_id=request.session_id,
            experiment_name=request.experiment_name,
            started_at=request.started_at.isoformat(),
            finished_at=None,
            status=SessionState.RECORDING.value,
            clinostat_run_id=None,
            sensor_mode=self._sensor_mode,
            pump_mode=PUMP_MODE,
            errors=(),
        )
        self._open_streams()
        self._persist_manifest()
        return self.snapshot()

    def update_run_id(self, request: SessionUpdateRequest) -> SessionSnapshot:
        self._require_active(request.session_id)
        if self._apply_run_id(request.clinostat_run_id):
            self._persist_manifest()
        return self.snapshot()

    def append_sensor(self, sample: SensorSample) -> None:
        self._require_active_any()
        row = [
            SCHEMA_VERSION, self._active.session_id, self._active.sensor_mode,
            sample.source_timestamp_ms, sample.session_elapsed_ms, sample.loop_count,
            sample.raw_adc, sample.resistance_ohm, sample.delta_r_over_r0,
            sample.temperature_c, sample.battery_pct,
        ]
        try:
            csv.writer(self._sensor_fh).writerow(row)
            self._sensor_count += 1
            self._maybe_flush_sensor()
        except OSError as exc:
            self._handle_io_fault(str(exc))
            raise SessionIoError(str(exc)) from exc

    def append_pump_event(self, event: PumpEvent) -> None:
        self._require_active_any()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self._active.session_id,
            "ts_ms": event.ts_ms,
            "previous_state": event.previous_state,
            "new_state": event.new_state,
            "cause": event.cause,
            "request_id": event.request_id,
            "delivered_volume_ul": event.delivered_volume_ul,
        }
        try:
            self._pump_fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._pump_count += 1
            if event.new_state in _FSYNC_ON_PUMP_STATES:
                self._pump_fh.flush()
                os.fsync(self._pump_fh.fileno())
                self._pump_count = 0
                self._pump_last_flush = self._clock()
            else:
                self._maybe_flush_pump()
        except OSError as exc:
            self._handle_io_fault(str(exc))
            raise SessionIoError(str(exc)) from exc

    def finish(self, request: SessionFinishRequest) -> SessionSnapshot:
        self._require_active(request.session_id)
        if request.clinostat_run_id is not None:
            self._apply_run_id(request.clinostat_run_id)

        final_status = self._active.status
        if final_status not in (SessionState.PARTIAL.value, SessionState.FAILED.value):
            final_status = SessionState.COMPLETED.value
        self._active = replace(
            self._active, status=final_status, finished_at=request.finished_at.isoformat(),
        )
        self._flush_and_fsync_all()
        self._persist_manifest()
        snapshot = self.snapshot()
        self._close_streams()
        self._active = None
        return snapshot

    def snapshot(self) -> SessionSnapshot:
        if self._active is None:
            raise SessionNotActiveError("no session is currently active")
        a = self._active
        return SessionSnapshot(
            schema_version=SCHEMA_VERSION, session_id=a.session_id,
            experiment_name=a.experiment_name, started_at=a.started_at,
            finished_at=a.finished_at, status=a.status,
            clinostat_run_id=a.clinostat_run_id, sensor_mode=a.sensor_mode,
            pump_mode=a.pump_mode, errors=a.errors,
        )

    # ─────────────────────────── 내부 헬퍼 ───────────────────────────

    def _require_active_any(self) -> None:
        if self._active is None:
            raise SessionNotActiveError("no session is currently active")

    def _require_active(self, session_id: str) -> None:
        self._require_active_any()
        if self._active.session_id != session_id:
            raise SessionConflictError(
                f"session {session_id!r} does not match active session "
                f"{self._active.session_id!r}"
            )

    def _apply_run_id(self, run_id: str) -> bool:
        """같은 run ID 재요청은 멱등, 다른 ID로의 교체는 409 (스펙 6.6)."""
        current = self._active.clinostat_run_id
        if current is not None and current != run_id:
            raise SessionConflictError(
                f"clinostat_run_id already set to {current!r}; "
                f"cannot replace with {run_id!r}"
            )
        if current == run_id:
            return False
        self._active = replace(self._active, clinostat_run_id=run_id)
        return True

    def _handle_io_fault(self, message: str) -> None:
        """기록 중 I/O 오류 -> 세션 partial 전이 + 오류 기록 (스펙 5)."""
        if self._active is None:
            return
        self._active = replace(
            self._active,
            status=SessionState.PARTIAL.value,
            errors=self._active.errors + (message,),
        )
        try:
            self._persist_manifest()
        except OSError:
            logger.exception("failed to persist manifest after io fault")

    def _open_streams(self) -> None:
        self._sensor_fh = open(
            self._session_dir / SENSOR_CSV_NAME, "w", newline="", encoding="utf-8",
        )
        csv.writer(self._sensor_fh).writerow(SENSOR_CSV_HEADER)
        self._sensor_fh.flush()
        self._sensor_count = 0
        self._sensor_last_flush = self._clock()

        self._pump_fh = open(self._session_dir / PUMP_JSONL_NAME, "w", encoding="utf-8")
        self._pump_count = 0
        self._pump_last_flush = self._clock()

    def _maybe_flush_sensor(self) -> None:
        now = self._clock()
        if (self._sensor_count >= FLUSH_RECORD_THRESHOLD
                or now - self._sensor_last_flush >= FLUSH_INTERVAL_S):
            self._sensor_fh.flush()
            self._sensor_count = 0
            self._sensor_last_flush = now

    def _maybe_flush_pump(self) -> None:
        now = self._clock()
        if (self._pump_count >= FLUSH_RECORD_THRESHOLD
                or now - self._pump_last_flush >= FLUSH_INTERVAL_S):
            self._pump_fh.flush()
            self._pump_count = 0
            self._pump_last_flush = now

    def _flush_and_fsync_all(self) -> None:
        for fh in (self._sensor_fh, self._pump_fh):
            if fh is not None:
                fh.flush()
                os.fsync(fh.fileno())

    def _close_streams(self) -> None:
        for fh in (self._sensor_fh, self._pump_fh):
            if fh is not None:
                fh.close()
        self._sensor_fh = None
        self._pump_fh = None

    def _persist_manifest(self) -> None:
        payload = asdict(self.snapshot())
        payload["errors"] = list(payload["errors"])
        _atomic_write_json(self._session_dir / MANIFEST_NAME, payload)
