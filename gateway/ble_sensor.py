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
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional, Protocol

from gateway.api_models import SensorSample
from gateway.ble_packet import BlePacketError, parse_sensor_packet
from gateway.sensor_source import SensorSourceError

logger = logging.getLogger("gateway.ble_sensor")

#: Arduino 펌웨어(`ble_service.h`)가 광고하는 커스텀 서비스. 데이터 특성은 ...AC.
SERVICE_UUID = "12345678-1234-1234-1234-1234567890ab"
SENSOR_DATA_UUID = "12345678-1234-1234-1234-1234567890ac"

DEFAULT_DEVICE_NAME = "ResistanceSensor"
DEFAULT_SCAN_TIMEOUT_S = 15.0

#: 수신 버퍼 상한. 소비(tick)가 멈춰도 메모리가 무한히 늘지 않게 한다.
#: 넘치면 **가장 오래된 것부터** 버린다 — 최신값이 화면·루프에 더 중요하다.
MAX_BUFFERED_SAMPLES = 2048

#: 연결이 끊긴 뒤 재연결을 몇 번까지 시도할지. 보드 전원 교체·리셋은 흔한 일이라
#: 한 번 끊겼다고 바로 오류로 올리지 않는다. 연속 실패가 이 값을 넘으면 FAULT.
MAX_RECONNECT_ATTEMPTS = 5


@dataclass(frozen=True)
class BleTarget:
    """어떤 장치에 붙을지. 우선순위는 address → service_uuid → name.

    ⚠ **이름으로 찾지 않는 것이 기본이다.** 2026-07-29 실기 확인 결과 이 센서는
    광고에 Local Name 을 넣지 않는다(MAC 만 보인다). 이름 기반 탐색
    (`find_device_by_name`)은 그래서 영원히 실패한다. 서비스 UUID 는 펌웨어가
    실제로 광고하는 계약이므로 이쪽이 맞다. 이름은 보조 필터로만 쓴다.
    """

    service_uuid: str = SERVICE_UUID
    address: Optional[str] = None
    name: Optional[str] = None

    def describe(self) -> str:
        if self.address:
            return f"address={self.address}"
        parts = [f"service={self.service_uuid}"]
        if self.name:
            parts.append(f"name={self.name!r}")
        return " ".join(parts)


class BleClient(Protocol):
    """BLE 연결 대상. 테스트는 이 인터페이스를 구현한 가짜를 주입한다."""

    async def connect(self, target: BleTarget, timeout: float) -> bool: ...

    def stream(self) -> AsyncIterator[bytes]: ...

    async def disconnect(self) -> None: ...


class BleSensorSource:
    """BLE notify 를 백그라운드로 받아 폴링 인터페이스로 노출한다."""

    def __init__(
        self,
        *,
        target: Optional[BleTarget] = None,
        scan_timeout_s: float = DEFAULT_SCAN_TIMEOUT_S,
        client_factory: Optional[Callable[[], BleClient]] = None,
        max_buffered: int = MAX_BUFFERED_SAMPLES,
        max_reconnects: int = MAX_RECONNECT_ATTEMPTS,
    ) -> None:
        self._target = target or BleTarget()
        self._scan_timeout_s = scan_timeout_s
        self._max_reconnects = max_reconnects
        self._reconnects = 0
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
        """연결 → 수신 → (끊기면) 재연결. 취소될 때까지 유지한다.

        보드 전원을 갈거나 리셋하면 BLE가 끊긴다. 재연결이 없으면 소스가 조용히
        멈춘 채 state만 running으로 남아, 운영자가 '센서가 느린 것'과 구분할 수 없다.
        **첫 연결 실패는 즉시 오류**(장치가 없다는 뜻)지만, 한 번 붙은 뒤의 끊김은
        일시적일 수 있으므로 backoff로 재시도하고 연속 실패가 쌓이면 오류로 올린다.
        """
        try:
            client = self._make_client()
            self._client = client

            if not await client.connect(self._target, self._scan_timeout_s):
                self._error = (
                    f"BLE device not found ({self._target.describe()}, "
                    f"scanned {self._scan_timeout_s:.0f}s) — 전원과 광고 상태를 확인하세요"
                )
                logger.warning("%s", self._error)
                return

            logger.info("BLE 센서 연결됨 (%s)", self._target.describe())
            failures = 0
            while True:
                async for payload in client.stream():
                    sample = self._to_sample(payload)
                    if sample is not None:
                        self._push(sample)
                    failures = 0          # 데이터가 흐르면 실패 카운트를 지운다

                # stream이 끝났다 = 연결이 끊겼다.
                failures += 1
                if failures > self._max_reconnects:
                    self._error = (
                        f"BLE 연결이 끊긴 뒤 재연결 {self._max_reconnects}회 모두 실패 "
                        f"({self._target.describe()})"
                    )
                    logger.warning("%s", self._error)
                    return
                delay = min(2.0 * failures, 10.0)
                logger.warning("BLE 연결 끊김 — %.0f초 후 재연결 시도 (%d/%d)",
                               delay, failures, self._max_reconnects)
                await asyncio.sleep(delay)
                with contextlib.suppress(Exception):
                    await client.disconnect()
                if await client.connect(self._target, self._scan_timeout_s):
                    self._reconnects += 1
                    logger.info("BLE 센서 재연결됨 (%s)", self._target.describe())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 tick()으로 알린다
            self._error = f"BLE stream failed ({self._target.describe()}): {exc}"
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
    """실제 bleak 백엔드. `BleClient` 프로토콜에 맞춘 얇은 어댑터."""

    def __init__(self) -> None:
        self._client: Any = None
        self._data_uuid = SENSOR_DATA_UUID
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._disconnected = asyncio.Event()

    async def connect(self, target: BleTarget, timeout: float) -> bool:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:  # pragma: no cover — 배포 환경 문제
            raise SensorSourceError("bleak is required for BLE_LIVE mode") from exc

        device = await self._discover(BleakScanner, target, timeout)
        if device is None:
            return False
        self._disconnected.clear()
        # 끊김을 이벤트로 받는다. 이게 없으면 stream()이 큐에서 영원히 대기해
        # 보드가 리셋돼도 아무도 모른다(2026-07-29 전원 교체 시험 전에 발견).
        self._client = BleakClient(
            device, disconnected_callback=lambda _c: self._disconnected.set())
        await self._client.connect()
        await self._client.start_notify(self._data_uuid, self._on_notify)
        return True

    @staticmethod
    async def _discover(scanner: Any, target: BleTarget, timeout: float) -> Any:
        """address → service UUID 순으로 찾는다. 이름은 동점일 때의 선호도일 뿐.

        이름 기반 탐색을 쓰지 않는 이유는 `BleTarget` 주석 참고 — 이 센서는
        광고에 Local Name 을 넣지 않는다.
        """
        if target.address:
            return await scanner.find_device_by_address(target.address, timeout=timeout)

        wanted = target.service_uuid.lower()
        found = await scanner.discover(timeout=timeout, return_adv=True)
        matches = [
            (device, adv)
            for device, adv in found.values()
            if wanted in {u.lower() for u in (adv.service_uuids or [])}
        ]
        if not matches:
            return None
        if target.name:
            for device, adv in matches:
                if (adv.local_name or device.name) == target.name:
                    return device
        if len(matches) > 1:
            logger.warning(
                "서비스 %s 를 광고하는 장치가 %d개 — 첫 번째를 쓴다. "
                "구분이 필요하면 ble.address 를 지정하세요: %s",
                target.service_uuid, len(matches),
                ", ".join(d.address for d, _ in matches),
            )
        return matches[0][0]

    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        self._queue.put_nowait(bytes(data))

    async def stream(self) -> AsyncIterator[bytes]:
        """끊길 때까지 패킷을 낸다. 끊기면 조용히 끝낸다 — 호출부가 재연결한다."""
        while not self._disconnected.is_set():
            getter = asyncio.ensure_future(self._queue.get())
            waiter = asyncio.ensure_future(self._disconnected.wait())
            done, pending = await asyncio.wait(
                {getter, waiter}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if getter in done:
                yield getter.result()
            else:
                return          # 끊김

    async def disconnect(self) -> None:
        if self._client is None:
            return
        client, self._client = self._client, None
        with contextlib.suppress(Exception):
            await client.stop_notify(self._data_uuid)
        with contextlib.suppress(Exception):
            await client.disconnect()
