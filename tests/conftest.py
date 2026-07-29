"""Gateway API/WebSocket 테스트 공용 fixture — tmp_path로 절대 경로를 덮어쓴다."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.runtime import GatewayConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = REPO_ROOT / "datasets"


def make_gw_config(tmp_path: Path, **overrides) -> GatewayConfig:
    defaults = dict(
        host="127.0.0.1",
        port=8010,
        sensor_publish_hz=10.0,
        browser_publish_hz=5.0,
        min_free_bytes=1024,
        datasets_dir=DATASETS_DIR,
        adc_full_scale=4095,
        reference_resistor_ohm=82_500.0,
        default_temperature_c=25.0,
        default_battery_pct=100,
        pump_backend=None,
        pump_min_volume_ul=1.0,
        pump_max_volume_ul=1000.0,
        pump_min_rate_ul_s=1.0,
        pump_max_rate_ul_s=200.0,
        data_root=tmp_path / "data",
        flush_interval_s=1.0,
        flush_record_count=100,
        state_root=tmp_path / "state",
    )
    defaults.update(overrides)
    return GatewayConfig(**defaults)


@pytest.fixture
def gw_config(tmp_path: Path) -> GatewayConfig:
    return make_gw_config(tmp_path)


@pytest.fixture
def client(gw_config: GatewayConfig):
    app = create_app(gw_config)
    with TestClient(app) as test_client:
        yield test_client
