"""저항센서 BLE 데이터 패킷 파서.

Arduino 펌웨어(`sensor_ble_firmware`)가 notify 로 보내는 21바이트 구조체를 푼다.
형식은 `LOC_circuit_design/software/ble_receiver/packet_parser.py` 와 동일하다 —
게이트웨이는 파이에 단독 배포되므로(그 경로가 없다) 계약을 여기 옮겨 둔다.
**펌웨어 struct 가 바뀌면 두 곳을 같이 고쳐야 한다.**

리틀엔디안 배치:
    uint32_t timestamp_ms      offset  0
    int32_t  raw_adc           offset  4
    float    resistance_ohm    offset  8
    float    delta_r_over_r0   offset 12
    float    temperature_c     offset 16
    uint8_t  battery_pct       offset 20
    합계 21바이트

시리얼 로그 형식(`serial_sensor.py`)과 달리 **raw_adc 가 실제로 들어 있다** —
그쪽은 없어서 None 으로 두지만 여기서는 측정값을 그대로 채운다.
"""

from __future__ import annotations

import math
import struct
from typing import Any, Dict

#: 리틀엔디안 uint32, int32, float×3, uint8
SENSOR_PACKET_FORMAT: str = "<IifffB"
SENSOR_PACKET_SIZE: int = struct.calcsize(SENSOR_PACKET_FORMAT)  # 21


class BlePacketError(ValueError):
    """패킷 길이·값이 계약을 벗어났다. 해당 패킷만 버리고 스트림은 유지한다."""

    def __init__(self, message: str, raw: bytes = b"") -> None:
        super().__init__(message)
        self.raw = bytes(raw)


def parse_sensor_packet(data: bytes) -> Dict[str, Any]:
    """21바이트 센서 패킷을 dict 로 푼다.

    형식은 맞지만 값이 비정상(무한대/NaN, 배터리 범위 밖, 음수 저항)이면 예외를
    올린다 — 조용히 통과시키면 잘못된 값이 세션 로그에 남는다.
    """
    if len(data) != SENSOR_PACKET_SIZE:
        raise BlePacketError(
            f"expected {SENSOR_PACKET_SIZE} bytes, got {len(data)}", data
        )

    try:
        unpacked = struct.unpack(SENSOR_PACKET_FORMAT, bytes(data))
    except struct.error as exc:
        raise BlePacketError(f"cannot unpack sensor packet: {exc}", data) from exc

    timestamp_ms, raw_adc, resistance_ohm, delta_r_over_r0, temperature_c, battery_pct = unpacked

    for name, value in (
        ("resistance_ohm", resistance_ohm),
        ("delta_r_over_r0", delta_r_over_r0),
        ("temperature_c", temperature_c),
    ):
        if not math.isfinite(value):
            raise BlePacketError(f"{name} must be finite: {value!r}", data)
    if not 0 <= battery_pct <= 100:
        raise BlePacketError(f"battery_pct out of range: {battery_pct}", data)
    if resistance_ohm < 0:
        raise BlePacketError(f"resistance_ohm must not be negative: {resistance_ohm}", data)

    return {
        "timestamp_ms": int(timestamp_ms),
        "raw_adc": int(raw_adc),
        "resistance_ohm": float(resistance_ohm),
        "delta_r_over_r0": float(delta_r_over_r0),
        "temperature_c": float(temperature_c),
        "battery_pct": int(battery_pct),
    }
