"""SpaceBio Gateway API 계약 — 설계 스펙 6장의 단일 진실 소스.

필드명은 snake_case, 시각은 timezone 포함 RFC 3339, 물리량은 필드명에 단위를 붙인다.
범위 밖 값은 clamp하지 않고 거부한다(422). 변경 요청은 unknown field를 금지한다.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 1
PUMP_MODE = "SIMULATED"
ESTOP_RESET_ACKNOWLEDGEMENT = "RESET_SIMULATED_PUMP_ESTOP"

#: 펌프 입력·저장 정밀도 (스펙 6.4)
VOLUME_DECIMALS = 3

#: Clinostat FastAPI가 생성하는 통합 세션 ID: spacebio_YYYYMMDD_HHMMSS_<suffix>
SESSION_ID_PATTERN = r"^spacebio_\d{8}_\d{6}_[A-Za-z0-9]+$"

#: dataset_id는 allowlist 키다. 경로 구분자·점·공백을 허용하면 traversal 표면이 된다.
DATASET_ID_PATTERN = r"^[A-Za-z0-9_-]+$"


# ─────────────────────────── enum ───────────────────────────

class GatewayState(str, Enum):
    ONLINE = "online"
    DEGRADED = "degraded"


class SensorState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    FAULT = "fault"


class PumpState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAULT = "fault"
    EMERGENCY_STOPPED = "emergency_stopped"


class SessionState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    RECORDING = "recording"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class SensorMode(str, Enum):
    CSV_REPLAY = "CSV_REPLAY"
    SYNTHETIC = "SYNTHETIC"


# ─────────────────────────── 공용 타입 ───────────────────────────

Finite = Field(allow_inf_nan=False)

RateUlS = Annotated[float, Field(ge=1.0, le=200.0, allow_inf_nan=False)]
VolumeUlS = Annotated[float, Field(ge=1.0, le=1000.0, allow_inf_nan=False)]
ReplaySpeed = Annotated[float, Field(ge=0.1, le=10.0, allow_inf_nan=False)]
BaselineOhm = Annotated[float, Field(ge=1_000.0, le=10_000_000.0, allow_inf_nan=False)]
PeriodS = Annotated[float, Field(ge=0.5, le=3600.0, allow_inf_nan=False)]
TemperatureC = Annotated[float, Field(ge=0.0, le=60.0, allow_inf_nan=False)]
BatteryPct = Annotated[int, Field(ge=0, le=100)]

#: 실제 센서 노드는 **ADS1115(16비트 차동)**라 raw ADC가 음수를 포함한 int16
#: 전 범위를 쓴다. ThinkPad 실측 세션에서 확인된 실제 범위는 -13263~12985다.
#: 스펙 6.3의 "0-4095"는 합성 생성기가 divider 역변환 결과를 clamp할 때 쓰는
#: **config 기본값**(SYNTHETIC_ADC_FULL_SCALE)이지 공용 저장 스키마의 제약이
#: 아니다. 여기에 4095를 걸면 실측 CSV 재생이 전부 거부된다.
RAW_ADC_MIN = -32768
RAW_ADC_MAX = 32767
RawAdc = Annotated[int, Field(ge=RAW_ADC_MIN, le=RAW_ADC_MAX)]

#: 합성 소스가 divider 역변환 결과를 clamp하는 기본 full-scale (스펙 6.3).
SYNTHETIC_ADC_FULL_SCALE = 4095
SYNTHETIC_REFERENCE_RESISTOR_OHM = 82_500.0
RequestId = Annotated[str, Field(min_length=1, max_length=128)]


class _Request(BaseModel):
    """모든 변경 요청의 기반 — unknown field 금지."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId


class _Status(BaseModel):
    model_config = ConfigDict(frozen=True)


def _round_volume(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, VOLUME_DECIMALS)


# ─────────────────────────── 센서 요청 ───────────────────────────

class SensorConfigureCsvRequest(_Request):
    """ThinkPad 실측 CSV 재생 설정 (스펙 6.3)."""

    mode: Literal[SensorMode.CSV_REPLAY] = SensorMode.CSV_REPLAY
    dataset_id: Annotated[str, Field(min_length=1, max_length=128,
                                     pattern=DATASET_ID_PATTERN)]
    replay_speed: ReplaySpeed = 1.0
    loop: bool = True


class SensorConfigureSyntheticRequest(_Request):
    """결정적 합성 저항신호 설정 (스펙 6.3).

    R = R0 + amplitude*sin(2πt/period) + seeded Gaussian noise
    """

    mode: Literal[SensorMode.SYNTHETIC] = SensorMode.SYNTHETIC
    baseline_resistance_ohm: BaselineOhm
    amplitude_ohm: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    period_s: PeriodS
    noise_std_ohm: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    seed: int
    temperature_c: TemperatureC = 25.0
    battery_pct: BatteryPct = 100

    @model_validator(mode="after")
    def _check_relative_bounds(self) -> "SensorConfigureSyntheticRequest":
        max_amplitude = self.baseline_resistance_ohm * 0.5
        if self.amplitude_ohm > max_amplitude:
            raise ValueError(
                f"amplitude_ohm must be <= 50% of baseline ({max_amplitude})"
            )
        max_noise = self.amplitude_ohm * 0.5
        if self.noise_std_ohm > max_noise:
            raise ValueError(
                f"noise_std_ohm must be <= 50% of amplitude ({max_noise})"
            )
        return self


SensorConfigureRequest = Annotated[
    Union[SensorConfigureCsvRequest, SensorConfigureSyntheticRequest],
    Field(discriminator="mode"),
]

_CONFIGURE_ADAPTER: TypeAdapter[Any] = TypeAdapter(SensorConfigureRequest)


def parse_sensor_configure(payload: dict[str, Any]):
    """`mode`로 CSV/합성 요청을 판별한다. 알 수 없는 mode는 ValidationError."""
    return _CONFIGURE_ADAPTER.validate_python(payload)


class SensorStartRequest(_Request):
    """본문은 request_id만 받는다 (스펙 6.3)."""


class SensorStopRequest(_Request):
    pass


# ─────────────────────────── 펌프 요청 ───────────────────────────

class PumpDispenseRequest(_Request):
    rate_ul_s: RateUlS
    target_volume_ul: VolumeUlS

    @field_validator("rate_ul_s", "target_volume_ul", mode="after")
    @classmethod
    def _quantize(cls, value: float) -> float:
        return round(value, VOLUME_DECIMALS)


class PumpStopRequest(_Request):
    pass


class PumpEmergencyStopRequest(_Request):
    pass


class PumpResetEmergencyStopRequest(_Request):
    """운영자 확인 문자열이 정확히 일치해야 한다. 틀리면 422 (스펙 6.5)."""

    acknowledgement: Literal[ESTOP_RESET_ACKNOWLEDGEMENT]


# ─────────────────────────── 세션 요청 ───────────────────────────

SessionId = Annotated[str, Field(pattern=SESSION_ID_PATTERN)]


class _NamedSessionRequest(_Request):
    session_id: SessionId


class SessionStartRequest(_NamedSessionRequest):
    experiment_name: Annotated[str, Field(min_length=1, max_length=200)]
    started_at: AwareDatetime

    @field_validator("experiment_name", mode="after")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("experiment_name must not be blank")
        return stripped


class SessionUpdateRequest(_NamedSessionRequest):
    """같은 run ID의 반복 update는 멱등, 다른 ID로의 교체는 409 (스펙 6.6)."""

    clinostat_run_id: Annotated[str, Field(min_length=1, max_length=200)]


class SessionFinishRequest(_NamedSessionRequest):
    finished_at: AwareDatetime
    clinostat_run_id: Optional[
        Annotated[str, Field(min_length=1, max_length=200)]
    ] = None


# ─────────────────────────── 상태 스키마 (6.2) ───────────────────────────

class SensorSample(_Status):
    source_timestamp_ms: int
    session_elapsed_ms: int
    loop_count: int = 0
    raw_adc: RawAdc
    resistance_ohm: Annotated[float, Field(allow_inf_nan=False)]
    delta_r_over_r0: Annotated[float, Field(allow_inf_nan=False)]
    temperature_c: Annotated[float, Field(allow_inf_nan=False)]
    battery_pct: BatteryPct


class PumpEvent(_Status):
    """펌프 상태 전이 1건 — `pump_events.jsonl`의 행 계약 (스펙 6.5).

    `simulated_pump`가 만들고 `session_store`가 기록한다. 두 모듈이 각자
    정의하면 필드가 어긋나므로(2026-07-24 실제로 `at` vs `ts_ms`로 갈렸다)
    **여기가 유일한 정의다.** 양쪽 모두 이것을 import해서 쓴다.
    """

    previous_state: PumpState
    new_state: PumpState
    cause: str
    request_id: str
    delivered_volume_ul: float
    at: AwareDatetime

    @property
    def ts_ms(self) -> int:
        """epoch 밀리초. JSONL에는 `at`(RFC 3339)과 함께 기록해 정렬을 쉽게 한다."""
        return int(self.at.timestamp() * 1000)


class GatewayComponent(_Status):
    state: GatewayState = GatewayState.ONLINE
    last_seen_at: Optional[AwareDatetime] = None


class SensorStatus(_Status):
    state: SensorState = SensorState.IDLE
    mode: Optional[SensorMode] = None
    sample: Optional[SensorSample] = None


class PumpStatus(_Status):
    mode: Literal["SIMULATED"] = PUMP_MODE
    state: PumpState = PumpState.IDLE
    estop_latched: bool = False
    rate_ul_s: float = 0.0
    target_volume_ul: Optional[float] = None
    delivered_volume_ul: float = 0.0
    session_cumulative_volume_ul: float = 0.0


class SessionStatus(_Status):
    state: SessionState = SessionState.IDLE
    session_id: Optional[str] = None
    experiment_name: Optional[str] = None


class GatewayStatus(_Status):
    gateway: GatewayComponent
    sensor: SensorStatus
    pump: PumpStatus
    session: SessionStatus


class StatusMessage(_Status):
    """WebSocket 상태 메시지. `sequence`는 프로세스 수명 동안 단조 증가한다."""

    type: Literal["status"] = "status"
    sequence: int
    data: GatewayStatus


# ─────────────────────────── 응답 envelope ───────────────────────────

_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:/[\w.\-]+){2,}")
_TRACEBACK_MARKERS = ("Traceback (most recent call last)", 'File "')
_GENERIC_ERROR = "internal error"


def server_time() -> str:
    """timezone 포함 RFC 3339 문자열."""
    return datetime.now(timezone.utc).astimezone().isoformat()


def _sanitize_message(message: str) -> str:
    """내부 traceback과 파일 경로가 클라이언트로 새지 않게 한다 (스펙 6.1)."""
    if any(marker in message for marker in _TRACEBACK_MARKERS):
        return _GENERIC_ERROR
    first_line = message.splitlines()[0] if message else ""
    return _PATH_RE.sub("<path>", first_line)[:500]


def success_envelope(*, request_id: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "server_time": server_time(),
        "data": data,
    }


def error_envelope(*, request_id: str, code: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "server_time": server_time(),
        "error": {"code": code, "message": _sanitize_message(message)},
    }
