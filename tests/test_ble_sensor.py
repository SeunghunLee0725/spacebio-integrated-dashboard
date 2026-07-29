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
from gateway.ble_sensor import BleSensorSource, BleTarget, _BleakClientAdapter
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
        self.target = None

    async def connect(self, target: BleTarget, timeout: float) -> bool:
        self.target = target
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
    src = BleSensorSource(target=BleTarget(name="ResistanceSensor"), client_factory=lambda: client)
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
    src = BleSensorSource(target=BleTarget(name="X"), client_factory=lambda: client)
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
    src = BleSensorSource(target=BleTarget(name="X"), client_factory=lambda: FakeBleClient([]))
    assert src.tick() is None                          # start() 전
    src.start()
    assert src.tick() is None                          # 아직 수신 없음
    await src.aclose()


@pytest.mark.asyncio
async def test_malformed_packet_is_dropped_not_fatal():
    """길이가 틀린 패킷 하나 때문에 스트림 전체가 끊기면 안 된다."""
    client = FakeBleClient([b"\x00" * 5, _packet(timestamp_ms=99)])
    src = BleSensorSource(target=BleTarget(name="X"), client_factory=lambda: client)
    src.start()
    samples = await _drain(src, 1)
    await src.aclose()

    assert len(samples) == 1
    assert samples[0].source_timestamp_ms == 99


@pytest.mark.asyncio
async def test_connect_failure_is_reported_through_tick():
    """스캔 실패는 조용히 넘기지 않는다 — 운영자가 알아야 한다."""
    src = BleSensorSource(
        target=BleTarget(name="Missing"),
        client_factory=lambda: FakeBleClient([], fail_connect=True),
        initial_attempts=1,          # 재시도 없이 바로 실패시켜 검증한다
    )
    src.start()
    await asyncio.sleep(0.05)
    with pytest.raises(SensorSourceError, match="Missing"):
        src.tick()
    await src.aclose()


@pytest.mark.asyncio
async def test_aclose_disconnects_client():
    client = FakeBleClient([_packet()])
    src = BleSensorSource(target=BleTarget(name="X"), client_factory=lambda: client)
    src.start()
    await asyncio.sleep(0.05)
    await src.aclose()
    assert client.disconnect_calls == 1
    assert client.connected is False


@pytest.mark.asyncio
async def test_start_is_idempotent_and_restarts_cleanly():
    client = FakeBleClient([_packet(timestamp_ms=7)])
    src = BleSensorSource(target=BleTarget(name="X"), client_factory=lambda: client)
    src.start()
    src.start()                                        # 두 번 불러도 태스크는 하나
    samples = await _drain(src, 1)
    await src.aclose()
    assert len(samples) == 1


# ─────────────────────────── 장치 탐색 (실기 결함 대응) ───────────────────────────
#
# 2026-07-29 실기 확인: 이 센서는 광고에 Local Name 을 넣지 않는다. MAC 만 보이고
# 커스텀 서비스 UUID 만 광고한다. 이름 기반 탐색은 그래서 영원히 실패한다.

from gateway.ble_sensor import SERVICE_UUID


class _Dev:
    def __init__(self, address, name=None):
        self.address = address
        self.name = name


class _Adv:
    def __init__(self, service_uuids, local_name=None):
        self.service_uuids = service_uuids
        self.local_name = local_name


class FakeScanner:
    """bleak.BleakScanner 의 discover/find_device_by_address 만 흉내낸다."""

    def __init__(self, found):
        self._found = found
        self.by_address_calls = []

    async def discover(self, timeout=None, return_adv=False):
        return self._found

    async def find_device_by_address(self, address, timeout=None):
        self.by_address_calls.append(address)
        for dev, _adv in self._found.values():
            if dev.address == address:
                return dev
        return None


@pytest.mark.asyncio
async def test_discovery_finds_device_by_service_uuid_without_name():
    """이름이 없어도 서비스 UUID 로 찾아야 한다 — 이게 실기 실패의 핵심이었다."""
    dev = _Dev("2D:62:81:2C:26:C2")
    scanner = FakeScanner({dev.address: (dev, _Adv([SERVICE_UUID]))})
    found = await _BleakClientAdapter._discover(scanner, BleTarget(), 5.0)
    assert found is dev


@pytest.mark.asyncio
async def test_discovery_ignores_devices_without_our_service():
    other = _Dev("AA:BB:CC:DD:EE:FF", name="누군가의 폰")
    scanner = FakeScanner({other.address: (other, _Adv(["0000fd69-0000-1000-8000-00805f9b34fb"]))})
    assert await _BleakClientAdapter._discover(scanner, BleTarget(), 5.0) is None


@pytest.mark.asyncio
async def test_discovery_matches_uuid_case_insensitively():
    """펌웨어는 대문자, bleak 는 소문자로 준다 — 대소문자로 놓치면 안 된다."""
    dev = _Dev("2D:62:81:2C:26:C2")
    scanner = FakeScanner({dev.address: (dev, _Adv([SERVICE_UUID.upper()]))})
    assert await _BleakClientAdapter._discover(scanner, BleTarget(), 5.0) is dev


@pytest.mark.asyncio
async def test_discovery_prefers_named_device_when_several_match():
    a, b = _Dev("11:11:11:11:11:11"), _Dev("22:22:22:22:22:22")
    scanner = FakeScanner({
        a.address: (a, _Adv([SERVICE_UUID], local_name="다른센서")),
        b.address: (b, _Adv([SERVICE_UUID], local_name="ResistanceSensor")),
    })
    found = await _BleakClientAdapter._discover(
        scanner, BleTarget(name="ResistanceSensor"), 5.0)
    assert found is b


@pytest.mark.asyncio
async def test_discovery_uses_address_when_given():
    """address 가 있으면 스캔보다 우선한다 — 센서를 여러 대 붙일 때의 확정 경로."""
    dev = _Dev("2D:62:81:2C:26:C2")
    scanner = FakeScanner({dev.address: (dev, _Adv([]))})   # UUID 광고가 없어도
    found = await _BleakClientAdapter._discover(
        scanner, BleTarget(address="2D:62:81:2C:26:C2"), 5.0)
    assert found is dev
    assert scanner.by_address_calls == ["2D:62:81:2C:26:C2"]


# ─────────────────────── 끊김·재연결 (전원 교체 대응) ───────────────────────
#
# 보드 전원을 갈거나 리셋하면 BLE 가 끊긴다. 재연결이 없으면 소스가 조용히 멈춘 채
# state 만 running 으로 남아, 운영자가 '느린 센서'와 구분할 수 없다.

class FlakyBleClient:
    """지정한 횟수만큼 스트림이 중간에 끊기는 클라이언트."""

    def __init__(self, rounds, *, reconnect_ok=True):
        self._rounds = list(rounds)      # 라운드별로 낼 패킷 목록
        self._reconnect_ok = reconnect_ok
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self, target, timeout: float) -> bool:
        self.connect_calls += 1
        if self.connect_calls == 1:
            return True
        return self._reconnect_ok

    async def stream(self):
        if not self._rounds:
            return                        # 더 낼 것이 없으면 끊긴 것으로 본다
        for p in self._rounds.pop(0):
            yield p
            await asyncio.sleep(0)
        return                            # 라운드 끝 = 연결 끊김

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


@pytest.mark.asyncio
async def test_reconnects_after_disconnect_and_keeps_streaming():
    client = FlakyBleClient([
        [_packet(timestamp_ms=10)],       # 1라운드 → 끊김
        [_packet(timestamp_ms=20)],       # 재연결 후 2라운드
    ])
    src = BleSensorSource(target=BleTarget(), client_factory=lambda: client,
                          max_reconnects=3)
    src.start()
    samples = await _drain(src, 2, timeout=6.0)
    await src.aclose()

    assert [s.source_timestamp_ms for s in samples] == [10, 20]
    assert client.connect_calls >= 2, "재연결을 시도해야 한다"


@pytest.mark.asyncio
async def test_gives_up_after_max_reconnects_and_reports_error():
    """무한 재시도로 조용히 매달려 있으면 안 된다 — 결국은 알려야 한다."""
    client = FlakyBleClient([[_packet()]], reconnect_ok=False)
    src = BleSensorSource(target=BleTarget(), client_factory=lambda: client,
                          max_reconnects=1)
    src.start()
    for _ in range(200):
        await asyncio.sleep(0.05)
        if src._error is not None:
            break
    with pytest.raises(SensorSourceError, match="재연결"):
        src.tick()
    await src.aclose()


@pytest.mark.asyncio
async def test_first_connect_failure_is_not_treated_as_reconnect():
    """처음부터 못 붙은 것은 '장치 없음'이지 '끊김'이 아니다 — 메시지가 달라야 한다."""
    src = BleSensorSource(target=BleTarget(name="Missing"),
                          client_factory=lambda: FakeBleClient([], fail_connect=True),
                          initial_attempts=1)
    src.start()
    await asyncio.sleep(0.05)
    with pytest.raises(SensorSourceError, match="not found"):
        src.tick()
    await src.aclose()


# ───────── 첫 연결 재시도 (BlueZ 간헐 타임아웃 대응, 2026-07-29 실기) ─────────


class FlakyFirstConnect:
    """처음 N번은 실패하고 그 뒤에 붙는 클라이언트."""

    def __init__(self, fail_times: int, *, raise_instead=False):
        self._left = fail_times
        self._raise = raise_instead
        self.attempts = 0

    async def connect(self, target, timeout: float) -> bool:
        self.attempts += 1
        if self._left > 0:
            self._left -= 1
            if self._raise:
                raise TimeoutError()
            return False
        return True

    async def stream(self):
        yield _packet(timestamp_ms=5)

    async def disconnect(self) -> None: ...


@pytest.mark.asyncio
async def test_retries_first_connect_before_giving_up():
    client = FlakyFirstConnect(fail_times=2)
    src = BleSensorSource(target=BleTarget(), client_factory=lambda: client,
                          initial_attempts=3)
    src.start()
    samples = await _drain(src, 1, timeout=12.0)
    await src.aclose()

    assert client.attempts == 3
    assert len(samples) == 1


@pytest.mark.asyncio
async def test_connect_timeout_is_retried_not_fatal():
    """BlueZ 는 실패를 False 가 아니라 TimeoutError 로도 낸다."""
    client = FlakyFirstConnect(fail_times=1, raise_instead=True)
    src = BleSensorSource(target=BleTarget(), client_factory=lambda: client,
                          initial_attempts=3)
    src.start()
    samples = await _drain(src, 1, timeout=12.0)
    await src.aclose()

    assert len(samples) == 1


@pytest.mark.asyncio
async def test_gives_up_after_initial_attempts_exhausted():
    client = FlakyFirstConnect(fail_times=99)
    src = BleSensorSource(target=BleTarget(), client_factory=lambda: client,
                          initial_attempts=2)
    src.start()
    for _ in range(200):
        await asyncio.sleep(0.05)
        if src._error is not None:
            break
    with pytest.raises(SensorSourceError, match="2회 시도"):
        src.tick()
    await src.aclose()
    assert client.attempts == 2


# ───── 재연결 빠르게 (실기: 45~150초마다 끊김, 매번 전체 스캔이면 20초 공백) ─────


@pytest.mark.asyncio
async def test_reconnect_uses_known_address_instead_of_full_scan():
    dev = _Dev("2D:62:81:2C:26:C2")
    scanner = FakeScanner({dev.address: (dev, _Adv([SERVICE_UUID]))})
    found = await _BleakClientAdapter._discover(
        scanner, BleTarget(), 15.0, known_address=dev.address)
    assert found is dev
    assert scanner.by_address_calls == [dev.address], "전체 스캔을 건너뛰어야 한다"


@pytest.mark.asyncio
async def test_reconnect_falls_back_to_full_scan_if_address_gone():
    """주소가 바뀌었을 수 있다 — 못 찾으면 전체 스캔으로 넘어가야 한다."""
    dev = _Dev("11:22:33:44:55:66")
    scanner = FakeScanner({dev.address: (dev, _Adv([SERVICE_UUID]))})
    found = await _BleakClientAdapter._discover(
        scanner, BleTarget(), 15.0, known_address="AA:AA:AA:AA:AA:AA")
    assert found is dev, "전체 스캔으로 되찾아야 한다"


@pytest.mark.asyncio
async def test_explicit_address_takes_priority_over_cached():
    a, b = _Dev("11:11:11:11:11:11"), _Dev("22:22:22:22:22:22")
    scanner = FakeScanner({a.address: (a, _Adv([SERVICE_UUID])),
                           b.address: (b, _Adv([SERVICE_UUID]))})
    found = await _BleakClientAdapter._discover(
        scanner, BleTarget(address=b.address), 15.0, known_address=a.address)
    assert found is b
