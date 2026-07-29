"""실기 센서 소스가 자원을 못 잡았을 때 상태가 FAULT로 드러나는지 검증.

예전에는 `tick()`이 올린 예외가 폴링 태스크를 조용히 죽였다. 상태는 계속
`running`으로 남고 샘플만 안 들어와, 운영자가 '센서가 느린 것'과 '아예 못 붙은 것'을
구분할 수 없었다. BLE는 스캔 실패가 흔하므로 이 구분이 특히 중요하다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gateway.api_models import SensorState
from gateway.runtime import GatewayRuntime
from gateway.sensor_source import SensorSourceError
from tests.conftest import make_gw_config


class FailingSource:
    """start()는 되지만 첫 tick()에서 자원 실패를 알리는 소스."""

    def __init__(self, message: str = "BLE device 'ResistanceSensor' not found"):
        self._message = message
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def tick(self):
        raise SensorSourceError(self._message)

    async def aclose(self) -> None:
        self.closed = True


class SilentSource:
    """정상이지만 아직 샘플이 없는 소스 — FAULT로 가면 안 된다."""

    def start(self) -> None: ...

    def tick(self):
        return None


@pytest.mark.asyncio
async def test_source_failure_marks_sensor_fault(tmp_path: Path):
    runtime = GatewayRuntime(make_gw_config(tmp_path, sensor_publish_hz=50.0))
    runtime._sensor_source = FailingSource()

    status = await runtime.start_sensor("r1")
    assert status.state == SensorState.RUNNING

    for _ in range(50):                     # 폴링 한 주기 이상 기다린다
        await asyncio.sleep(0.02)
        if (await runtime.status()).sensor.state == SensorState.FAULT:
            break

    assert (await runtime.status()).sensor.state == SensorState.FAULT


@pytest.mark.asyncio
async def test_sensor_without_samples_stays_running(tmp_path: Path):
    """샘플이 아직 없는 것과 실패는 다르다 — 조용한 센서를 FAULT로 만들면 안 된다."""
    runtime = GatewayRuntime(make_gw_config(tmp_path, sensor_publish_hz=50.0))
    runtime._sensor_source = SilentSource()

    await runtime.start_sensor("r1")
    await asyncio.sleep(0.2)

    assert (await runtime.status()).sensor.state == SensorState.RUNNING
    await runtime.stop_sensor("r2")


@pytest.mark.asyncio
async def test_stop_releases_source_but_keeps_it_configured(tmp_path: Path):
    """정지 시 자원은 놓되 소스 객체는 남겨 재시작이 되게 한다."""
    runtime = GatewayRuntime(make_gw_config(tmp_path))
    source = FailingSource()
    runtime._sensor_source = source

    await runtime.start_sensor("r1")
    await runtime.stop_sensor("r2")

    assert source.closed is True                    # 자원은 놓았고
    assert runtime._sensor_source is source         # 설정은 남아 있다
