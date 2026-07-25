"""실기 저항센서 — Nano 33 BLE(nRF52840) 시리얼 스트림 (실측 연동).

파이의 `/dev/ttyACM0`에 USB로 직결된 센서 노드가 사람이 읽는 로그 형식을 낸다.

    [Data] t=15821367ms  R=1000000.0Ω  ΔR/R0=0.000%  T=25.0°C  Bat=0%  BLE=disconnected

ThinkPad 쪽 `serial_receiver`가 기대하는 CSV 형식과 다르다(보드 펌웨어가 다른
모드다). 펌웨어를 건드리지 않고 이 형식을 그대로 받는다.

⚠ 이 형식에는 `raw_adc`가 없다. 저항값에서 역산하면 측정하지 않은 값을
측정한 것처럼 기록하게 되므로 **None으로 남긴다.** 저장 CSV에서는 빈 칸이 된다.

⚠ 관측된 발행 주기는 약 0.2 Hz로 설계 스펙의 10 Hz보다 훨씬 느리다. 폴링
주기와 무관하게 새 줄이 올 때만 샘플을 만든다.
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Callable, Optional

from gateway.api_models import SensorSample
from gateway.sensor_source import SensorSourceError

logger = logging.getLogger("gateway.serial_sensor")

DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_BAUDRATE = 115200

#: `[Data] t=..ms R=..Ω ΔR/R0=..% T=..°C Bat=..% BLE=..`
#: 단위 기호(Ω, °C)는 인코딩이 흔들릴 수 있어 숫자 뒤를 느슨하게 받는다.
_DATA_RE = re.compile(
    r"\[Data\]\s*"
    r"t=(?P<t>-?\d+)\s*ms\s+"
    r"R=(?P<r>-?[\d.]+(?:[eE][-+]?\d+)?)\s*\S*\s+"
    r"[^=]*=(?P<d>-?[\d.]+(?:[eE][-+]?\d+)?)\s*%\s+"
    r"T=(?P<temp>-?[\d.]+)\s*\S*\s+"
    r"Bat=(?P<bat>\d+)\s*%"
)


def parse_data_line(line: str) -> Optional[dict]:
    """센서 로그 한 줄을 파싱한다. `[Data]` 줄이 아니면 None.

    형식은 맞지만 값이 비정상(무한대, 배터리 범위 밖)이면 예외를 올린다 —
    조용히 넘기면 잘못된 값이 세션 로그에 남는다.
    """
    match = _DATA_RE.search(line)
    if match is None:
        return None

    timestamp_ms = int(match.group("t"))
    resistance_ohm = float(match.group("r"))
    # 펌웨어는 백분율로 낸다. 저장 계약은 비율이므로 100으로 나눈다.
    delta_r_over_r0 = float(match.group("d")) / 100.0
    temperature_c = float(match.group("temp"))
    battery_pct = int(match.group("bat"))

    for name, value in (("resistance_ohm", resistance_ohm),
                        ("delta_r_over_r0", delta_r_over_r0),
                        ("temperature_c", temperature_c)):
        if not math.isfinite(value):
            raise SensorSourceError(f"{name} must be finite: {line.strip()!r}")
    if not 0 <= battery_pct <= 100:
        raise SensorSourceError(f"battery_pct out of range: {battery_pct}")
    if resistance_ohm < 0:
        raise SensorSourceError(f"resistance_ohm must not be negative: {resistance_ohm}")

    return {
        "timestamp_ms": timestamp_ms,
        "resistance_ohm": resistance_ohm,
        "delta_r_over_r0": delta_r_over_r0,
        "temperature_c": temperature_c,
        "battery_pct": battery_pct,
    }


class SerialSensorSource:
    """시리얼 포트를 논블로킹으로 읽어 새 줄이 있을 때만 샘플을 낸다.

    `serial_factory`를 주입하면 실제 포트 없이 테스트할 수 있다.
    """

    def __init__(
        self,
        *,
        port: str = DEFAULT_PORT,
        baudrate: int = DEFAULT_BAUDRATE,
        serial_factory: Optional[Callable[[], object]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._serial_factory = serial_factory
        self._clock = clock
        self._serial = None
        self._started_at: Optional[float] = None
        self._first_timestamp_ms: Optional[int] = None

    def start(self) -> None:
        """포트를 열고 경과 기준을 초기화한다. 이미 열려 있으면 버퍼만 비운다."""
        if self._serial is None:
            self._serial = self._open()
        try:
            self._serial.reset_input_buffer()
        except Exception:  # noqa: BLE001 — 가짜 포트는 이 메서드가 없을 수 있다
            pass
        self._started_at = self._clock()
        self._first_timestamp_ms = None

    def _open(self):
        if self._serial_factory is not None:
            return self._serial_factory()
        try:
            import serial
        except ImportError as exc:  # pragma: no cover
            raise SensorSourceError("pyserial is required for the serial sensor") from exc
        try:
            return serial.Serial(self._port, self._baudrate, timeout=0)
        except Exception as exc:
            raise SensorSourceError(f"cannot open {self._port}: {exc}") from exc

    def tick(self) -> Optional[SensorSample]:
        """새 `[Data]` 줄이 있으면 샘플을, 없으면 None을 돌려준다."""
        if self._serial is None or self._started_at is None:
            return None

        sample = None
        while True:                       # 밀린 줄이 있으면 가장 최근 것을 쓴다
            raw = self._serial.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace")
            try:
                parsed = parse_data_line(line)
            except SensorSourceError as exc:
                logger.warning("dropping malformed sensor line: %s", exc)
                continue
            if parsed is None:
                continue
            sample = self._to_sample(parsed)
        return sample

    def _to_sample(self, parsed: dict) -> SensorSample:
        if self._first_timestamp_ms is None:
            self._first_timestamp_ms = parsed["timestamp_ms"]
        elapsed_ms = parsed["timestamp_ms"] - self._first_timestamp_ms
        return SensorSample(
            source_timestamp_ms=parsed["timestamp_ms"],
            session_elapsed_ms=max(0, elapsed_ms),
            loop_count=0,
            raw_adc=None,                 # 이 형식은 raw ADC를 내지 않는다
            resistance_ohm=parsed["resistance_ohm"],
            delta_r_over_r0=parsed["delta_r_over_r0"],
            temperature_c=parsed["temperature_c"],
            battery_pct=parsed["battery_pct"],
        )

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001
                logger.warning("failed to close %s", self._port)
            self._serial = None
