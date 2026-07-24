"""Gateway 런타임 — 센서 소스·모의 펌프·세션 저장소를 하나로 묶는다 (설계 스펙 6장).

FastAPI 핸들러(`gateway/app.py`, `gateway/routes.py`)는 검증·위임·envelope 포장만
하고, 실제 상태 전이는 전부 여기서 일어난다. 소스·펌프·저장소 변경은 단일
`asyncio.Lock`으로 직렬화한다 — 센서 재설정이 진행 중인 세션 기록과 겹치거나,
세션 시작/종료가 센서 tick 루프와 겹치는 것을 막기 위해서다.

⚠ 안전 핵심: `startup()`은 `recover_session()`으로 메타데이터만 복원한다.
센서·펌프 실행은 절대 자동 재개하지 않는다 (`start_sensor`/pump dispense를
호출하지 않는다). 비상정지 래치는 `SimulatedPump` 자신의
`FileEstopLatchPersistence`가 생성 시점에 독립적으로 복원한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Union

from gateway.api_models import (
    GatewayComponent,
    GatewayState,
    GatewayStatus,
    PumpDispenseRequest,
    PumpEmergencyStopRequest,
    PumpEvent,
    PumpResetEmergencyStopRequest,
    PumpState,
    PumpStatus,
    PumpStopRequest,
    SensorConfigureCsvRequest,
    SensorConfigureSyntheticRequest,
    SensorMode,
    SensorSample,
    SensorState,
    SensorStatus,
    SessionFinishRequest,
    SessionStartRequest,
    SessionState,
    SessionStatus,
    SessionUpdateRequest,
    server_time,
)
from gateway.csv_replay import load_csv_replay_source
from gateway.idempotency import IdempotencyCache
from gateway.recovery import RecoveryResult, recover_session
from gateway.session_store import (
    InsufficientSpaceError,
    SessionConflictError,
    SessionIoError,
    SessionNotActiveError,
    SessionSnapshot,
    SessionStore,
)
from gateway.runtime_config import GatewayConfig, load_config
from gateway.simulated_pump import FileEstopLatchPersistence, SimulatedPump
from gateway.synthetic_sensor import SyntheticSensorSource

logger = logging.getLogger("gateway.runtime")

#: WebSocket/센서 상태 발행 상한 (설계 스펙 6장) — config가 이보다 높은 값을
#: 줘도 여기서 clamp한다.
MAX_PUBLISH_HZ = 10.0

_ESTOP_LATCH_FILENAME = "estop_latch.json"

SensorConfigCommand = Union[SensorConfigureCsvRequest, SensorConfigureSyntheticRequest]
PumpCommand = Union[
    PumpDispenseRequest, PumpStopRequest, PumpEmergencyStopRequest,
    PumpResetEmergencyStopRequest,
]
SessionCommand = Union[SessionStartRequest, SessionUpdateRequest, SessionFinishRequest]


class SensorConflictError(Exception):
    """센서 상태 전이 오류 — 409에 대응 (미설정 상태 시작, 실행 중 재설정 등)."""

    def __init__(self, message: str, *, code: str = "sensor_conflict") -> None:
        super().__init__(message)
        self.code = code


def _find_latest_session_dir(sessions_root: Path) -> Optional[Path]:
    """가장 최근에 수정된 세션 디렉터리를 찾는다 — 재시작 시 reconciliation 대상."""
    if not sessions_root.exists():
        return None
    candidates = [d for d in sessions_root.iterdir() if d.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


class GatewayRuntime:
    """소스·펌프·저장소를 소유하고 변경을 단일 lock으로 직렬화하는 오케스트레이터."""

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._started_monotonic = time.monotonic()
        self._lock = asyncio.Lock()
        self._sequence = itertools.count(1)

        self._sensor_state = SensorState.IDLE
        self._sensor_mode: Optional[SensorMode] = None
        self._sensor_source: Optional[Any] = None
        self._sensor_sample: Optional[SensorSample] = None
        self._sensor_task: Optional[asyncio.Task[None]] = None

        self._session_cache = IdempotencyCache()
        self._active_store: Optional[SessionStore] = None
        self._current_session_status = SessionStatus()
        self.recovery: Optional[RecoveryResult] = None

        estop_path = self._config.state_root / _ESTOP_LATCH_FILENAME
        self._pump = SimulatedPump(
            event_sink=self._dispatch_pump_event,
            estop_persistence=FileEstopLatchPersistence(estop_path),
        )

    # ─────────────────────────── 라이프사이클 ───────────────────────────

    async def startup(self) -> None:
        """`recover_session()`으로 메타데이터만 복원한다 — 실행은 재개하지 않는다."""
        sessions_root = self._config.data_root / "sessions"
        latest = _find_latest_session_dir(sessions_root)
        if latest is None:
            return
        try:
            self.recovery = recover_session(latest)
        except OSError:
            logger.exception("session recovery failed for %s", latest)
            return
        try:
            state = SessionState(self.recovery.status)
        except ValueError:
            state = SessionState.FAILED
        self._current_session_status = SessionStatus(
            state=state, session_id=self.recovery.session_id, experiment_name=None,
        )

    async def shutdown(self) -> None:
        """백그라운드 센서 tick task를 cancel하고 await한다."""
        task = self._sensor_task
        self._sensor_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def uptime_s(self) -> float:
        return time.monotonic() - self._started_monotonic

    def next_sequence(self) -> int:
        """WS 상태 메시지 sequence — 프로세스 수명 동안 단조 증가(연결마다 리셋 아님)."""
        return next(self._sequence)

    # ─────────────────────────── 조회 ───────────────────────────

    async def status(self) -> GatewayStatus:
        async with self._lock:
            return await self._status_locked()

    async def _status_locked(self) -> GatewayStatus:
        pump_status = await self._pump.status()
        gateway_state = (
            GatewayState.DEGRADED if pump_status.state == PumpState.FAULT
            else GatewayState.ONLINE
        )
        return GatewayStatus(
            gateway=GatewayComponent(state=gateway_state, last_seen_at=server_time()),
            sensor=self._sensor_status_locked(),
            pump=pump_status,
            session=self._current_session_status,
        )

    async def subscribe(self) -> AsyncIterator[GatewayStatus]:
        """최대 `MAX_PUBLISH_HZ`로 상태를 계속 발행한다. 첫 값은 즉시 낸다."""
        interval = 1.0 / min(self._config.sensor_publish_hz, MAX_PUBLISH_HZ)
        while True:
            yield await self.status()
            await asyncio.sleep(interval)

    async def session_status_for_request(
        self, *, session_id: Optional[str], request_id: Optional[str],
    ) -> SessionStatus:
        """reconciliation 조회 — 같은 request_id 재요청이면 그때 응답을 그대로 낸다.

        모르는 세션을 조회하면 타임아웃만 보고 "시작 안 됐다"고 넘겨짚지 않도록
        명시적으로 빈 상태(state=idle, session_id=None)를 낸다 — 현재/최근 세션과
        다른 `session_id`를 물으면서 그 세션의 상태를 받은 것으로 착각하면 안
        된다(스펙 6.6).
        """
        async with self._lock:
            if request_id is not None:
                cached = self._session_cache.get(request_id)
                if cached is not None:
                    return cached
            if session_id is not None and session_id != self._current_session_status.session_id:
                return SessionStatus()
            return self._current_session_status

    # ─────────────────────────── 센서 ───────────────────────────

    def _sensor_status_locked(self) -> SensorStatus:
        return SensorStatus(
            state=self._sensor_state, mode=self._sensor_mode, sample=self._sensor_sample,
        )

    async def configure_sensor(self, request: SensorConfigCommand) -> SensorStatus:
        async with self._lock:
            if self._sensor_state == SensorState.RUNNING:
                raise SensorConflictError("sensor is running; stop before reconfiguring")

            if isinstance(request, SensorConfigureCsvRequest):
                manifest_path = self._config.datasets_dir / "manifest.json"
                source: Any = load_csv_replay_source(
                    request.dataset_id,
                    datasets_dir=self._config.datasets_dir,
                    manifest_path=manifest_path,
                    replay_speed=request.replay_speed,
                    loop=request.loop,
                )
                mode = SensorMode.CSV_REPLAY
            else:
                source = SyntheticSensorSource(
                    baseline_resistance_ohm=request.baseline_resistance_ohm,
                    amplitude_ohm=request.amplitude_ohm,
                    period_s=request.period_s,
                    noise_std_ohm=request.noise_std_ohm,
                    seed=request.seed,
                    temperature_c=request.temperature_c,
                    battery_pct=request.battery_pct,
                    reference_resistor_ohm=self._config.reference_resistor_ohm,
                    adc_full_scale=self._config.adc_full_scale,
                )
                mode = SensorMode.SYNTHETIC

            self._sensor_source = source
            self._sensor_mode = mode
            self._sensor_state = SensorState.IDLE
            self._sensor_sample = None
            return self._sensor_status_locked()

    async def start_sensor(self, request_id: str) -> SensorStatus:
        del request_id  # 센서 시작은 멱등이 요구되지 않는다 — 재시작 요청은 409.
        async with self._lock:
            if self._sensor_source is None:
                raise SensorConflictError("sensor is not configured")
            if self._sensor_state == SensorState.RUNNING:
                raise SensorConflictError("sensor is already running")
            self._sensor_source.start()
            self._sensor_state = SensorState.RUNNING
            self._sensor_sample = None
            self._sensor_task = asyncio.create_task(self._sensor_poll_loop())
            return self._sensor_status_locked()

    async def stop_sensor(self, request_id: str) -> SensorStatus:
        """멱등 — 이미 멈춰 있으면 에러 없이 현재 상태를 낸다."""
        del request_id
        task: Optional[asyncio.Task[None]] = None
        async with self._lock:
            if self._sensor_state == SensorState.RUNNING:
                self._sensor_state = SensorState.STOPPED
                task = self._sensor_task
                self._sensor_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        async with self._lock:
            return self._sensor_status_locked()

    async def _sensor_poll_loop(self) -> None:
        interval = 1.0 / self._config.sensor_publish_hz
        try:
            while True:
                await asyncio.sleep(interval)
                async with self._lock:
                    if self._sensor_source is None:
                        return
                    sample = self._sensor_source.tick()
                    if sample is None:
                        continue
                    self._sensor_sample = sample
                    if self._active_store is not None:
                        try:
                            self._active_store.append_sensor(sample)
                        except SessionIoError:
                            logger.exception("failed to append sensor sample to session store")
        except asyncio.CancelledError:
            raise

    # ─────────────────────────── 펌프 ───────────────────────────

    def _dispatch_pump_event(self, event: PumpEvent) -> None:
        """활성 세션이 있으면 그대로 저장소로 전달한다 — 변환 없이 직접 연결."""
        if self._active_store is not None:
            self._active_store.append_pump_event(event)

    async def pump_command(self, command: PumpCommand) -> Union[PumpStatus, dict[str, Any]]:
        async with self._lock:
            if isinstance(command, PumpDispenseRequest):
                return await self._pump.dispense(command)
            if isinstance(command, PumpStopRequest):
                return await self._pump.stop(command)
            if isinstance(command, PumpEmergencyStopRequest):
                return await self._pump.emergency_stop(command)
            if isinstance(command, PumpResetEmergencyStopRequest):
                return await self._reset_emergency_stop_locked(command)
            raise TypeError(f"unsupported pump command: {type(command)!r}")

    async def _reset_emergency_stop_locked(
        self, command: PumpResetEmergencyStopRequest,
    ) -> dict[str, Any]:
        """PumpStatus 전체 + `previous_state`/`accepted`(스펙 6.5)를 합쳐 낸다."""
        result = await self._pump.reset_emergency_stop(command)
        status = await self._pump.status()
        return {
            **status.model_dump(mode="json"),
            "previous_state": result.previous_state.value,
            "accepted": result.accepted,
        }

    # ─────────────────────────── 세션 ───────────────────────────

    def _status_from_snapshot(self, snapshot: SessionSnapshot) -> SessionStatus:
        return SessionStatus(
            state=SessionState(snapshot.status), session_id=snapshot.session_id,
            experiment_name=snapshot.experiment_name,
        )

    async def session_command(self, command: SessionCommand) -> SessionStatus:
        async with self._lock:
            cached = self._session_cache.get(command.request_id)
            if cached is not None:
                return cached

            if isinstance(command, SessionStartRequest):
                status = await self._session_start_locked(command)
            elif isinstance(command, SessionUpdateRequest):
                status = self._session_update_locked(command)
            elif isinstance(command, SessionFinishRequest):
                status = await self._session_finish_locked(command)
            else:
                raise TypeError(f"unsupported session command: {type(command)!r}")

            self._session_cache.set(command.request_id, status)
            self._current_session_status = status
            return status

    async def _session_start_locked(self, command: SessionStartRequest) -> SessionStatus:
        if self._active_store is not None:
            raise SessionConflictError(
                f"a session is already active: {self._active_store.snapshot().session_id}"
            )
        mode = self._sensor_mode or SensorMode.CSV_REPLAY
        store = SessionStore(
            self._config.data_root, sensor_mode=mode, min_free_bytes=self._config.min_free_bytes,
        )
        snapshot = store.start(command)
        self._active_store = store
        await self._pump.begin_session()
        return self._status_from_snapshot(snapshot)

    def _session_update_locked(self, command: SessionUpdateRequest) -> SessionStatus:
        if self._active_store is None:
            raise SessionNotActiveError("no session is currently active")
        snapshot = self._active_store.update_run_id(command)
        return self._status_from_snapshot(snapshot)

    async def _session_finish_locked(self, command: SessionFinishRequest) -> SessionStatus:
        """정지·종료는 멱등이어야 한다 — 같은 request_id 재시도는 위 캐시가 처리한다."""
        if self._active_store is None:
            raise SessionNotActiveError("no session is currently active")
        snapshot = self._active_store.finish(command)
        self._active_store = None
        await self._pump.end_session()
        return self._status_from_snapshot(snapshot)


__all__ = [
    "GatewayConfig",
    "GatewayRuntime",
    "SensorConflictError",
    "load_config",
]
