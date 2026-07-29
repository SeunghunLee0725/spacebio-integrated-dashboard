"""모의 펌프 안전 상태기계 (설계 스펙 6.4/6.5).

상태: idle -> running -> completed | stopped | fault | emergency_stopped.
전달량은 time.monotonic_ns() 기반 경과 시간 x 유량으로 계산한다 (sleep 없음,
clock은 주입 가능). 변경은 단일 asyncio.Lock으로 직렬화하고, 상태는 매번
새로 만든 불변 PumpStatus 스냅샷으로 반환한다 (기존 프로젝트의 frozen
pydantic 모델 관례를 따름 — gateway/api_models.py 참조).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from gateway.api_models import (
    PumpDispenseRequest,
    PumpEmergencyStopRequest,
    PumpEvent,
    PumpResetEmergencyStopRequest,
    PumpState,
    PumpStatus,
    PumpStopRequest,
    server_time,
)
from gateway.idempotency import IdempotencyCache

logger = logging.getLogger("gateway.simulated_pump")

#: 완료 판정 허용오차 (스펙 6.4): 목표량의 0.1% 또는 0.1 µL 중 큰 값.
_MIN_TOLERANCE_UL = 0.1
_TOLERANCE_FRACTION = 0.001


class PumpConflictError(Exception):
    """409 semantics: 동시 dispense 또는 래치 중 비허용 요청."""

    def __init__(self, message: str, *, code: str = "pump_conflict") -> None:
        super().__init__(message)
        self.code = code


class PumpFaultError(Exception):
    """내부 계산/저장 실패로 fault 전이가 일어났을 때 호출자에게 알린다."""

    def __init__(self, message: str, *, cause: BaseException) -> None:
        super().__init__(message)
        self.__cause__ = cause


@dataclass(frozen=True)
class ResetEmergencyStopResult:
    """reset-emergency-stop 응답 (스펙 6.5)."""

    previous_state: PumpState
    state: PumpState
    estop_latched: bool
    accepted: bool


EventSink = Callable[[PumpEvent], None]


def _noop_sink(event: PumpEvent) -> None:
    return None


class EstopLatchPersistence(Protocol):
    """비상정지 래치 영속화 경로. Gateway/파이 재시작 후에도 래치가 유지되게 한다."""

    def load(self) -> bool: ...
    def save(self, latched: bool) -> None: ...


class FileEstopLatchPersistence:
    """JSON 파일 기반 기본 구현. 임시파일에 쓰고 rename해 원자적으로 반영한다."""

    def __init__(self, path: "str | Path") -> None:
        self._path = Path(path)

    def load(self) -> bool:
        if not self._path.exists():
            return False
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return bool(data.get("estop_latched", False))

    def save(self, latched: bool) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"estop_latched": latched}))
        tmp.replace(self._path)


class _NullEstopLatchPersistence:
    """영속화 경로가 없을 때의 기본값 — 래치는 프로세스 수명 동안만 유지된다."""

    def load(self) -> bool:
        return False

    def save(self, latched: bool) -> None:
        return None


class SimulatedPump:
    """단일 모의 펌프의 안전 상태기계. 모든 변경은 asyncio.Lock으로 직렬화한다."""

    def __init__(
        self,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        event_sink: EventSink = _noop_sink,
        estop_persistence: Optional[EstopLatchPersistence] = None,
        idempotency_cache: Optional[IdempotencyCache] = None,
    ) -> None:
        self._monotonic_ns = monotonic_ns
        self._event_sink = event_sink
        self._estop_persistence = estop_persistence or _NullEstopLatchPersistence()
        self._idempotency = idempotency_cache or IdempotencyCache()
        self._lock = asyncio.Lock()

        self._estop_latched = self._estop_persistence.load()
        self._state = PumpState.EMERGENCY_STOPPED if self._estop_latched else PumpState.IDLE
        self._rate_ul_s = 0.0
        self._target_volume_ul: Optional[float] = None
        self._delivered_volume_ul = 0.0
        self._session_active = False
        self._session_cumulative_volume_ul = 0.0
        self._dispense_start_ns: Optional[int] = None
        self._active_request_id: Optional[str] = None

    # ─────────────────────────── 조회 ───────────────────────────

    async def status(self) -> PumpStatus:
        async with self._lock:
            self._update_progress_locked()
            return self._snapshot_locked()

    # ─────────────────────────── 세션 경계 ───────────────────────────

    async def begin_session(self) -> None:
        """새 세션 시작 — 세션 누적량을 0으로 초기화한다."""
        async with self._lock:
            self._session_active = True
            self._session_cumulative_volume_ul = 0.0

    async def end_session(self) -> None:
        async with self._lock:
            self._session_active = False

    # ─────────────────────────── 주입 ───────────────────────────

    async def dispense(self, request: PumpDispenseRequest) -> PumpStatus:
        async with self._lock:
            # 래치 검사가 멱등 캐시 조회보다 먼저다: 캐시된 request_id 재전송도
            # "펌프 변경 요청"이라서(스펙 6.5), 래치 중이면 과거 성공 스냅샷을 재생하지 않고
            # 409로 거부해야 UI가 실제로는 emergency_stopped인 펌프를 RUNNING으로 오인하지 않는다.
            if self._estop_latched:
                raise PumpConflictError(
                    "pump is emergency-stop latched; dispense is not allowed",
                    code="pump_estop_latched",
                )

            cached = self._idempotency.get(request.request_id)
            if cached is not None:
                return cached

            self._update_progress_locked()
            if self._state == PumpState.RUNNING:
                raise PumpConflictError(
                    "a dispense is already in progress", code="pump_dispense_in_progress"
                )

            previous = self._state
            self._state = PumpState.RUNNING
            self._rate_ul_s = request.rate_ul_s
            self._target_volume_ul = request.target_volume_ul
            self._delivered_volume_ul = 0.0
            self._dispense_start_ns = self._monotonic_ns()
            self._active_request_id = request.request_id
            self._emit_event(previous, self._state, cause="dispense_start",
                              request_id=request.request_id)

            snapshot = self._snapshot_locked()
            self._idempotency.set(request.request_id, snapshot)
            return snapshot

    async def stop(self, request: PumpStopRequest) -> PumpStatus:
        async with self._lock:
            self._update_progress_locked()
            if self._state == PumpState.RUNNING:
                self._finalize_dispense_locked(
                    final_state=PumpState.STOPPED, cause="stop", request_id=request.request_id
                )
            return self._snapshot_locked()

    async def emergency_stop(self, request: PumpEmergencyStopRequest) -> PumpStatus:
        async with self._lock:
            if self._estop_latched:
                # 정지·비상정지는 멱등이어야 한다(스펙 6장)는 요구가 6.5의 허용목록보다
                # 우선한다: 운영자가 estop을 두 번 누르거나 UI의 STOP ALL이 재시도해도
                # 에러 없이 현재 스냅샷을 반환한다 (감사 기록용으로 별도 cause를 남긴다).
                self._emit_event(self._state, self._state, cause="emergency_stop_repeat",
                                  request_id=request.request_id)
                return self._snapshot_locked()

            self._update_progress_locked()
            previous = self._state
            if previous == PumpState.RUNNING:
                # 물리적으로 이미 전달된 양이므로 영속화 성공 여부와 무관하게 반영한다.
                self._accumulate_session_delivered_locked()

            try:
                self._estop_persistence.save(True)
            except Exception as exc:  # noqa: BLE001 — 영속화 실패는 명시적으로 fault로 승격
                self._estop_latched = True  # fail-safe: 저장 실패해도 안전 쪽으로 가정
                self._state = PumpState.FAULT
                self._emit_event(previous, self._state, cause="fault",
                                  request_id=request.request_id)
                raise PumpFaultError("failed to persist e-stop latch", cause=exc) from exc

            self._estop_latched = True
            self._state = PumpState.EMERGENCY_STOPPED
            self._active_request_id = None
            self._emit_event(previous, self._state, cause="emergency_stop",
                              request_id=request.request_id)
            return self._snapshot_locked()

    async def reset_emergency_stop(
        self, request: PumpResetEmergencyStopRequest
    ) -> ResetEmergencyStopResult:
        """`request.acknowledgement`는 이미 pydantic Literal로 정확 일치 검증됨(422는
        api_models.PumpResetEmergencyStopRequest 생성 시점에 이미 일어난다)."""
        async with self._lock:
            previous_state = self._state
            if not self._estop_latched:
                return ResetEmergencyStopResult(
                    previous_state=previous_state, state=previous_state,
                    estop_latched=False, accepted=True,
                )

            try:
                self._estop_persistence.save(False)
            except Exception as exc:  # noqa: BLE001
                self._state = PumpState.FAULT
                self._emit_event(previous_state, self._state, cause="fault",
                                  request_id=request.request_id)
                raise PumpFaultError("failed to persist e-stop reset", cause=exc) from exc

            self._estop_latched = False
            self._state = PumpState.IDLE
            self._active_request_id = None
            self._emit_event(previous_state, self._state, cause="reset_emergency_stop",
                              request_id=request.request_id)
            return ResetEmergencyStopResult(
                previous_state=previous_state, state=PumpState.IDLE,
                estop_latched=False, accepted=True,
            )

    # ─────────────────────────── 내부 ───────────────────────────

    def _update_progress_locked(self) -> None:
        if self._state != PumpState.RUNNING:
            return
        assert self._dispense_start_ns is not None
        elapsed_s = (self._monotonic_ns() - self._dispense_start_ns) / 1_000_000_000
        delivered = elapsed_s * self._rate_ul_s
        target = self._target_volume_ul or 0.0
        tolerance = max(_MIN_TOLERANCE_UL, target * _TOLERANCE_FRACTION)
        if delivered >= target - tolerance:
            self._delivered_volume_ul = target  # 초과 금지 — 정확히 목표량으로 cap
            self._finalize_dispense_locked(
                final_state=PumpState.COMPLETED, cause="dispense_completed",
                request_id=self._active_request_id or "",
            )
        else:
            self._delivered_volume_ul = delivered

    def _accumulate_session_delivered_locked(self) -> None:
        if self._session_active:
            self._session_cumulative_volume_ul += self._delivered_volume_ul

    def _finalize_dispense_locked(self, *, final_state: PumpState, cause: str,
                                   request_id: str) -> None:
        previous = self._state
        self._accumulate_session_delivered_locked()
        self._state = final_state
        self._active_request_id = None
        self._emit_event(previous, final_state, cause=cause, request_id=request_id)

    def _snapshot_locked(self) -> PumpStatus:
        return PumpStatus(
            state=self._state,
            estop_latched=self._estop_latched,
            rate_ul_s=self._rate_ul_s,
            target_volume_ul=self._target_volume_ul,
            delivered_volume_ul=self._delivered_volume_ul,
            session_cumulative_volume_ul=self._session_cumulative_volume_ul,
        )

    def _emit_event(self, previous: PumpState, new: PumpState, *, cause: str,
                     request_id: str) -> None:
        event = PumpEvent(
            previous_state=previous, new_state=new, cause=cause, request_id=request_id,
            delivered_volume_ul=self._delivered_volume_ul, at=server_time(),
        )
        try:
            self._event_sink(event)
        except Exception:  # noqa: BLE001 — 이벤트 기록 실패가 상태기계를 막으면 안 된다
            logger.exception("event sink failed for cause=%s", cause)
