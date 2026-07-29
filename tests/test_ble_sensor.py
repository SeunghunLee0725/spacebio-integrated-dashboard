"""실기 저항센서 BLE 소스 — 가짜 BLE 클라이언트로 검증(실제 보드 불필요)."""

from __future__ import annotations

import asyncio
import struct

import pytest

from gateway.ble_packet import (
    SENSOR_PACKET_FORMAT,
    SENSOR_PACKET_SIZE,
    BlePacketError,
    parse_sensor_packet,
)
from gateway.ble_sensor import BleSensorSource
from gateway.sensor_source import SensorSourceError


def _packet(
    *,
    timestamp_ms: int = 15_821_367,
    raw_adc: int = 12_345,
    resistance_ohm: float = 253_000.0,
    delta_r_over_r0: float = 0.031,
    temperature_c: float = 25.4,
    battery_pct: int = 87,
) -> bytes:
    return struct.pack(
        SENSOR_PACKET_FORMAT,
        timestamp_ms,
        raw_adc,
        resistance_ohm,
        delta_r_over_r0,
        temperature_c,
        battery_pct,
    )


# ─────────────────────────── 패킷 파서 ───────────────────────────


def test_packet_size_matches_firmware_struct():
    """펌웨어 struct는 21바이트다. 여기가 어긋나면 전 패킷이 깨진다."""
    assert SENSOR_PACKET_SIZE == 21


def test_parse_sensor_packet_reads_all_fields():
    p = parse_sensor_packet(_packet())
    assert p["timestamp_ms"] == 15_821_367
    assert p["raw_adc"] == 12_345
    assert abs(p["resistance_ohm"] - 253_000.0) < 1e-3
    assert abs(p["delta_r_over_r0"] - 0.031) < 1e-6
    assert abs(p["temperature_c"] - 25.4) < 1e-4
    assert p["battery_pct"] == 87


def test_parse_sensor_packet_rejects_wrong_length():
    with pytest.raises(BlePacketError):
        parse_sensor_packet(b"\x00" * 20)


def test_parse_sensor_packet_rejects_out_of_range_battery():
    with pytest.raises(BlePacketError):
        parse_sensor_packet(_packet(battery_pct=200))


def test_parse_sensor_packet_rejects_negative_resistance():
    with pytest.raises(BlePacketError):
        parse_sensor_packet(_packet(resistance_ohm=-1.0))


def test_parse_sensor_packet_rejects_non_finite():
    with pytest.raises(BlePacketError):
        parse_sensor_packet(_packet(temperature_c=float("nan")))


# ─────────────────────────── 가짜 BLE 클라이언트 ───────────────────────────


class FakeBleClient:
    """notify 콜백에 미리 준비한 패킷을 흘려 주는 가짜 클라이언트."""

    def __init__(self, packets, *, fail_connect: bool = False):
        self._packets = list(packets)
        self._fail_connect = fail_connect
        self.connected = False
        self.disconnect_calls = 0
        self.device_name = None

    async def connect(self, device_name: str, timeout: float) -> bool:
        self.device_name = device_name
        if self._fail_connect:
            return False
        self.connected = True
        return True

    async def stream(self):
        for p in self._packets:
            yield p
            await asyncio.sleep(0)

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False


async def _drain(src: BleSensorSource, expected: int, *, timeout: float = 1.0):
    """tick()이 expected개를 낼 때까지 이벤트 루프를 양보하며 모은다."""
    out = []
    deadline = asyncio.get_running_loop().time() + timeout
    while len(out) < expected and asyncio.get_running_loop().time() < deadline:
        s = src.tick()
        if s is None:
            await asyncio.sleep(0.01)
            continue
        out.append(s)
    return out


# ─────────────────────────── 소스 동작 ───────────────────────────


@pytest.mark.asyncio
async def test_source_yields_samples_and_rebases_elapsed():
    client = FakeBleClient([
        _packet(timestamp_ms=1_000, resistance_ohm=253_000.0),
        _packet(timestamp_ms=1_500, resistance_ohm=252_000.0),
    ])
    src = BleSensorSource(device_name="ResistanceSensor", client_factory=lambda: client)
    src.start()
    samples = await _drain(src, 2)
    await src.aclose()

    assert len(samples) == 2
    assert samples[0].source_timestamp_ms == 1_000
    assert samples[0].session_elapsed_ms == 0          # 첫 샘플 기준으로 재정렬
    assert samples[1].session_elapsed_ms == 500
    assert samples[0].raw_adc == 12_345                # BLE는 시리얼과 달리 raw_adc가 있다
    assert abs(samples[1].resistance_ohm - 252_000.0) < 1e-3


@pytest.mark.asyncio
async def test_tick_returns_one_sample_per_call():
    """한 번에 최신까지 비우면 세션 로그에서 중간 샘플이 유실된다."""
    client = FakeBleClient([_packet(timestamp_ms=t) for t in (10, 20, 30)])
    src = BleSensorSource(device_name="X", client_factory=lambda: client)
    src.start()
    await asyncio.sleep(0.05)
    first = src.tick()
    second = src.tick()
    await src.aclose()

    assert first is not None and second is not None
    assert first.source_timestamp_ms == 10
    assert second.source_timestamp_ms == 20


@pytest.mark.asyncio
async def test_tick_returns_none_before_any_packet():
    src = BleSensorSource(device_name="X", client_factory=lambda: FakeBleClient([]))
    assert src.tick() is None                          # start() 전
    src.start()
    assert src.tick() is None                          # 아직 수신 없음
    await src.aclose()


@pytest.mark.asyncio
async def test_malformed_packet_is_dropped_not_fatal():
    """길이가 틀린 패킷 하나 때문에 스트림 전체가 끊기면 안 된다."""
    client = FakeBleClient([b"\x00" * 5, _packet(timestamp_ms=99)])
    src = BleSensorSource(device_name="X", client_factory=lambda: client)
    src.start()
    samples = await _drain(src, 1)
    await src.aclose()

    assert len(samples) == 1
    assert samples[0].source_timestamp_ms == 99


@pytest.mark.asyncio
async def test_connect_failure_is_reported_through_tick():
    """스캔 실패는 조용히 넘기지 않는다 — 운영자가 알아야 한다."""
    src = BleSensorSource(
        device_name="Missing",
        client_factory=lambda: FakeBleClient([], fail_connect=True),
    )
    src.start()
    await asyncio.sleep(0.05)
    with pytest.raises(SensorSourceError, match="Missing"):
        src.tick()
    await src.aclose()


@pytest.mark.asyncio
async def test_aclose_disconnects_client():
    client = FakeBleClient([_packet()])
    src = BleSensorSource(device_name="X", client_factory=lambda: client)
    src.start()
    await asyncio.sleep(0.05)
    await src.aclose()
    assert client.disconnect_calls == 1
    assert client.connected is False


@pytest.mark.asyncio
async def test_start_is_idempotent_and_restarts_cleanly():
    client = FakeBleClient([_packet(timestamp_ms=7)])
    src = BleSensorSource(device_name="X", client_factory=lambda: client)
    src.start()
    src.start()                                        # 두 번 불러도 태스크는 하나
    samples = await _drain(src, 1)
    await src.aclose()
    assert len(samples) == 1
