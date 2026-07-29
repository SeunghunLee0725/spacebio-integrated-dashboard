"""실기 저항센서 — Nano 33 BLE(nRF52840) BLE 직결 (BLE_LIVE).

`serial_sensor.py`(USB 시리얼)와 같은 센서의 **무선 경로**다. 둘의 차이:

| | SERIAL_LIVE | BLE_LIVE |
|---|---|---|
| 경로 | USB `/dev/ttyACM*` | BLE notify |
| 형식 | 사람이 읽는 `[Data]` 로그 | 21바이트 struct |
| `raw_adc` | **없음**(None) | **있음** |
| 주기 | 관측 약 0.2 Hz | 펌웨어 sampling_rate 설정값 |

**구조**: bleak 는 async 인데 `SensorSource` 계약의 `tick()` 은 동기다. 그래서
BLE 수신은 백그라운드 asyncio 태스크가 맡아 큐에 쌓고, `tick()` 은 큐에서 하나씩
꺼내기만 한다(논블로킹). 스레드는 쓰지 않는다 — 게이트웨이가 이미 asyncio 라
같은 루프에 태스크를 얹는 편이 단순하고 종료 처리도 확실하다.

⚠ `tick()` 은 **호출당 샘플 하나만** 낸다. 최신까지 비우면 세션 로그에서 중간
샘플이 유실된다(`serial_sensor.py` 와 같은 이유).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from typing import Any, AsyncIterator, Callable, Optional, Protocol

from gateway.api_models import SensorSample
from gateway.ble_packet import BlePacketError, parse_sensor_packet
from gateway.sensor_source import SensorSourceError

logger = logging.getLogger("gateway.ble_sensor")

DEFAULT_DEVICE_NAME = "ResistanceSensor"
DEFAULT_SCAN_TIMEOUT_S = 15.0

#: 수신 버퍼 상한. 소비(tick)가 멈춰도 메모리가 무한히 늘지 않게 한다.
#: 넘치면 **가장 오래된 것부터** 버린다 — 최신값이 화면·루프에 더 중요하다.
MAX_BUFFERED_SAMPLES = 2048


class BleClient(Protocol):
    """BLE 연결 대상. 테스트는 이 인터페이스를 구현한 가짜를 주입한다."""

    async def connect(self, device_name: str, timeout: float) -> bool: ...

    def stream(self) -> AsyncIterator[bytes]: ...

    async def disconnect(self) -> None: ...


class BleSensorSource:
    """BLE notify 를 백그라운드로 받아 폴링 인터페이스로 노출한다."""

    def __init__(
        self,
        *,
        device_name: str = DEFAULT_DEVICE_NAME,
        scan_timeout_s: float = DEFAULT_SCAN_TIMEOUT_S,
        client_factory: Optional[Callable[[], BleClient]] = None,
        max_buffered: int = MAX_BUFFERED_SAMPLES,
    ) -> None:
        self._device_name = device_name
        self._scan_timeout_s = scan_timeout_s
        self._client_factory = client_factory
        self._buffer: deque[SensorSample] = deque(maxlen=max_buffered)
        self._task: Optional[asyncio.Task[None]] = None
        self._client: Optional[BleClient] = None
        self._error: Optional[str] = None
        self._first_timestamp_ms: Optional[int] = None
        self._dropped = 0

    # ── SensorSource 계약 ────────────────────────────────────────────

    def start(self) -> None:
        """수신 태스크를 띄운다. 이미 떠 있으면 아무것도 하지 않는다(멱등)."""
        if self._task is not None and not self._task.done():
            return
        self._buffer.clear()
        self._error = None
        self._first_timestamp_ms = None
        self._dropped = 0
        self._task = asyncio.get_running_loop().create_task(self._run())

    def tick(self) -> Optional[SensorSample]:
        """받아둔 샘플이 있으면 하나, 없으면 None.

        연결 자체가 실패했으면 예외를 올린다 — 조용히 None 만 돌려주면
        운영자가 '센서가 느린 것'과 '아예 못 붙은 것'을 구분할 수 없다.
        """
        if self._error is not None:
            raise SensorSourceError(self._error)
        if not self._buffer:
            return None
        return self._buffer.popleft()

    async def aclose(self) -> None:
        """수신 태스크를 멈추고 BLE 연결을 끊는다."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._disconnect()

    def close(self) -> None:
        """동기 종료 경로 — 태스크만 취소한다. 가능하면 `aclose()` 를 쓸 것."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    # ── 내부 ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            client = self._make_client()
            self._client = client
            connected = await client.connect(self._device_name, self._scan_timeout_s)
            if not connected:
                self._error = (
                    f"BLE device {self._device_name!r} not found "
                    f"(scanned {self._scan_timeout_s:.0f}s) — 전원과 광고 상태를 확인하세요"
                )
                logger.warning("%s", self._error)
                return
            logger.info("BLE 센서 %r 연결됨", self._device_name)
            async for payload in client.stream():
                sample = self._to_sample(payload)
                if sample is not None:
                    self._push(sample)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 tick()으로 알린다
            self._error = f"BLE stream failed for {self._device_name!r}: {exc}"
            logger.exception("BLE 수신 실패")

    def _make_client(self) -> BleClient:
        if self._client_factory is not None:
            return self._client_factory()
        return _BleakClientAdapter()

    def _to_sample(self, payload: bytes) -> Optional[SensorSample]:
        """패킷 하나를 샘플로. 깨진 패킷은 버리고 스트림은 유지한다."""
        try:
            parsed = parse_sensor_packet(payload)
        except BlePacketError as exc:
            self._dropped += 1
            logger.warning("dropping malformed BLE packet (%d dropped): %s",
                           self._dropped, exc)
            return None

        if self._first_timestamp_ms is None:
            self._first_timestamp_ms = parsed["timestamp_ms"]
        elapsed_ms = parsed["timestamp_ms"] - self._first_timestamp_ms
        return SensorSample(
            source_timestamp_ms=parsed["timestamp_ms"],
            session_elapsed_ms=max(0, elapsed_ms),
            loop_count=0,
            raw_adc=parsed["raw_adc"],      # 시리얼 경로와 달리 실측값이 있다
            resistance_ohm=parsed["resistance_ohm"],
            delta_r_over_r0=parsed["delta_r_over_r0"],
            temperature_c=parsed["temperature_c"],
            battery_pct=parsed["battery_pct"],
        )

    def _push(self, sample: SensorSample) -> None:
        if len(self._buffer) == self._buffer.maxlen:
            logger.warning("BLE 수신 버퍼 초과 — 가장 오래된 샘플을 버린다")
        self._buffer.append(sample)

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.disconnect()


class _BleakClientAdapter:
    """실제 bleak 백엔드. `BleClient` 프로토콜에 맞춘 얇은 어댑터.

    UUID 는 Arduino 펌웨어(`ble_service.h`)와 `ble_receiver/ble_client.py` 가
    공유하는 값이다.
    """

    SERVICE_UUID = "12345678-1234-1234-1234-1234567890AB"
    SENSOR_DATA_UUID = "12345678-1234-1234-1234-1234567890AC"

    def __init__(self) -> None:
        self._client: Any = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def connect(self, device_name: str, timeout: float) -> bool:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:  # pragma: no cover — 배포 환경 문제
            raise SensorSourceError("bleak is required for BLE_LIVE mode") from exc

        device = await BleakScanner.find_device_by_name(device_name, timeout=timeout)
        if device is None:
            return False
        self._client = BleakClient(device)
        await self._client.connect()
        await self._client.start_notify(self.SENSOR_DATA_UUID, self._on_notify)
        return True

    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        self._queue.put_nowait(bytes(data))

    async def stream(self) -> AsyncIterator[bytes]:
        while True:
            yield await self._queue.get()

    async def disconnect(self) -> None:
        if self._client is None:
            return
        client, self._client = self._client, None
        with contextlib.suppress(Exception):
            await client.stop_notify(self.SENSOR_DATA_UUID)
        with contextlib.suppress(Exception):
            await client.disconnect()
