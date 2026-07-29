"""SpaceBio Gateway API 계약 — 설계 스펙 6장의 단일 진실 소스.

필드명은 snake_case, 시각은 timezone 포함 RFC 3339, 물리량은 필드명에 단위를 붙인다.
범위 밖 값은 clamp하지 않고 거부한다(422). 변경 요청은 unknown field를 금지한다.
"""

from __future__ import annotations

import re
import uuid
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
    #: 실기 — Nano 33 BLE(nRF52840)가 /dev/ttyACM*으로 흘리는 실측 스트림.
    #: 사람이 읽는 [Data] 로그 형식이라 raw_adc가 없다.
    SERIAL_LIVE = "SERIAL_LIVE"
    #: 실기 — 같은 센서의 **무선 경로**. 21바이트 struct를 BLE notify로 받는다.
    #: 시리얼과 달리 raw_adc가 들어 있다.
    BLE_LIVE = "BLE_LIVE"


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


def new_request_id() -> str:
    return str(uuid.uuid4())


class _Request(BaseModel):
    """모든 변경 요청의 기반 — unknown field 금지.

    `request_id`는 **선택**이다. 스펙 6장은 "모든 변경 요청에는 요청 ID를
    허용한다"이지 요구한다가 아니다. 브라우저는 `X-Request-ID` 헤더로만 보내고
    본문에는 넣지 않으므로(스펙 6.1), 필수로 두면 화면에서 오는 모든 변경
    요청이 422가 된다 — 2026-07-24 종단 스모크에서 실제로 발생했다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId = Field(default_factory=new_request_id)


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


class SensorConfigureSerialLiveRequest(_Request):
    """실기 센서 설정 — 파라미터가 없다. 보드 스트림을 그대로 받는다."""

    mode: Literal[SensorMode.SERIAL_LIVE] = SensorMode.SERIAL_LIVE


class SensorConfigureBleLiveRequest(_Request):
    """실기 센서 BLE 직결 설정.

    기본 탐색은 **서비스 UUID**로 한다 — 이 센서는 광고에 Local Name을 넣지 않아
    이름으로는 찾을 수 없다(2026-07-29 실기 확인). 센서를 여러 대 붙일 때만
    `address`로 특정한다. 비우면 config.yaml의 `ble.*` 값을 쓴다.
    """

    mode: Literal[SensorMode.BLE_LIVE] = SensorMode.BLE_LIVE
    #: BLE MAC (예: "2D:62:81:2C:26:C2"). 지정하면 UUID 탐색보다 우선한다.
    address: Optional[Annotated[str, Field(pattern=r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")]] = None
    #: 보조 필터 — 같은 서비스를 광고하는 장치가 여럿일 때만 의미가 있다.
    device_name: Optional[Annotated[str, Field(min_length=1, max_length=64)]] = None
    scan_timeout_s: Annotated[float, Field(gt=0.0, le=120.0)] = 15.0
    #: 레퍼런스 저항(Ω). **장치 설정이다** — 보드에 써 넣으면 펌웨어가 저항을
    #: 다시 계산하고 baseline 을 재설정한다. 비우면 config.yaml 값, 그것도 없으면
    #: 보드에 마지막으로 쓰인 값을 그대로 쓴다(장치 설정을 건드리지 않음).
    rref_ohm: Optional[Annotated[float, Field(ge=100.0, le=1_000_000.0)]] = None
    #: 시간평균 계수. 원시 N개를 하나로 평균한다(출력 주파수 = fs/N).
    avg_factor: Optional[Annotated[int, Field(ge=1, le=200)]] = None


SensorConfigureRequest = Annotated[
    Union[
        SensorConfigureCsvRequest,
        SensorConfigureSyntheticRequest,
        SensorConfigureSerialLiveRequest,
        SensorConfigureBleLiveRequest,
    ],
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

#: 무선 펌프 보드(ESP32-C6) 펌웨어 clampSpm 범위 (firmware SPM_MIN/SPM_MAX).
PUMP_SPM_MIN = 1
PUMP_SPM_MAX = 2400
PumpSpm = Annotated[int, Field(ge=PUMP_SPM_MIN, le=PUMP_SPM_MAX)]
PumpSteps = Annotated[int, Field(ge=1, le=100_000)]


class PumpStepRequest(_Request):
    """실기 무선 펌프의 스텝 단위 명령 (µL 보정이 없어 스텝으로 노출).

    펌웨어는 부피가 아니라 스텝으로만 돈다. µL 보정 상수가 확보되기 전까지
    부피로 표기하면 거짓이 되므로 스텝을 그대로 쓴다.
    """

    steps: PumpSteps
    spm: PumpSpm = 1200


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
    #: 실기 센서(SERIAL_LIVE)의 [Data] 로그 형식에는 raw ADC가 없다. 저항값에서
    #: 역산하면 측정하지 않은 값을 지어내는 셈이라 None으로 둔다. CSV 재생과
    #: 합성 모드는 값을 채운다. 저장 CSV에서는 빈 칸이 된다.
    raw_adc: Optional[RawAdc] = None
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
    #: 실기 BLE 모드에서 실제로 적용 중인 설정. 화면이 자기가 보낸 값을 되뇌지 않고
    #: 서버가 쓰고 있는 값을 그대로 보여주도록 여기서 알려준다.
    rref_ohm: Optional[float] = None
    avg_factor: Optional[int] = None


#: 무선 실기 펌프 백엔드가 붙었을 때의 mode 값.
PUMP_MODE_WIRELESS = "WIRELESS"


class PumpStatus(_Status):
    #: SIMULATED = 모의, WIRELESS = 실기 MQTT 펌프 보드.
    mode: str = PUMP_MODE
    state: PumpState = PumpState.IDLE
    estop_latched: bool = False
    rate_ul_s: float = 0.0
    target_volume_ul: Optional[float] = None
    delivered_volume_ul: float = 0.0
    session_cumulative_volume_ul: float = 0.0
    #: 실기 스텝 텔레메트리 (펌웨어 status의 pos/spm/run). 모의 모드에서는 None.
    position_steps: Optional[int] = None
    spm: Optional[int] = None


class DatasetInfo(_Status):
    """CSV 재생용 dataset 한 건의 공개 메타 — 경로·파일명은 노출하지 않는다."""

    dataset_id: str
    sample_count: int
    provenance: str


class DatasetsResponse(_Status):
    datasets: tuple[DatasetInfo, ...]


class SessionStatus(_Status):
    state: SessionState = SessionState.IDLE
    session_id: Optional[str] = None
    experiment_name: Optional[str] = None
    #: 이 세션의 데이터가 실제로 쌓이는 디렉터리(<data_root>/sessions/<session_id>).
    #: 화면이 파이 경로를 하드코딩하지 않도록 서버가 알려준다.
    data_dir: Optional[str] = None


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
