"""Gateway 런타임 설정 — `config.yaml`의 `gateway/sensor/pump/store/coordinator`
섹션을 `GatewayConfig`로 읽어들인다 (설계 스펙 6장 고정값).

`gateway/runtime.py`(`GatewayRuntime`)가 이 모듈의 `GatewayConfig`/`load_config`를
쓴다. 파일을 나눈 이유는 순전히 크기 때문이다 — 설정 로딩은 런타임 상태기계와
독립적인 관심사다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass(frozen=True)
class GatewayConfig:
    """`config.yaml`의 `gateway/sensor/pump/store/coordinator` 섹션 (설계 스펙 고정값)."""

    host: str = "127.0.0.1"
    port: int = 8010
    sensor_publish_hz: float = 10.0
    browser_publish_hz: float = 5.0
    min_free_bytes: int = 524_288_000
    datasets_dir: Path = Path("./datasets")
    adc_full_scale: int = 4095
    reference_resistor_ohm: float = 82_500.0
    default_temperature_c: float = 25.0
    default_battery_pct: int = 100
    pump_backend: Optional[str] = None
    pump_min_volume_ul: float = 1.0
    pump_max_volume_ul: float = 1000.0
    pump_min_rate_ul_s: float = 1.0
    pump_max_rate_ul_s: float = 200.0
    data_root: Path = Path("/home/aiworker-1/spacebio-data")
    flush_interval_s: float = 1.0
    flush_record_count: int = 100
    state_root: Path = Path("/home/aiworker-1/clinostat/spacebio-state")


def load_config(path: Path) -> GatewayConfig:
    """`config.yaml`을 읽어 `GatewayConfig`를 만든다. 없는 섹션은 스펙 기본값을 쓴다."""
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    gw = raw.get("gateway") or {}
    sensor = raw.get("sensor") or {}
    pump = raw.get("pump") or {}
    limits = pump.get("limits") or {}
    store = raw.get("store") or {}
    coordinator = raw.get("coordinator") or {}

    host = str(gw.get("host", "127.0.0.1"))
    if host == "0.0.0.0":  # noqa: S104 — 여기선 값 검사일 뿐, 바인딩이 아니다
        raise ValueError("gateway must bind to 127.0.0.1 only; 0.0.0.0 is forbidden")

    return GatewayConfig(
        host=host,
        port=int(gw.get("port", 8010)),
        sensor_publish_hz=float(gw.get("sensor_publish_hz", 10)),
        browser_publish_hz=float(gw.get("browser_publish_hz", 5)),
        min_free_bytes=int(gw.get("min_free_bytes", 524_288_000)),
        datasets_dir=Path(sensor.get("datasets_dir", "./datasets")),
        adc_full_scale=int(sensor.get("adc_full_scale", 4095)),
        reference_resistor_ohm=float(sensor.get("reference_resistor_ohm", 82_500.0)),
        default_temperature_c=float(sensor.get("default_temperature_c", 25.0)),
        default_battery_pct=int(sensor.get("default_battery_pct", 100)),
        pump_backend=pump.get("backend"),
        pump_min_volume_ul=float(limits.get("min_volume_ul", 1.0)),
        pump_max_volume_ul=float(limits.get("max_volume_ul", 1000.0)),
        pump_min_rate_ul_s=float(limits.get("min_rate_ul_s", 1.0)),
        pump_max_rate_ul_s=float(limits.get("max_rate_ul_s", 200.0)),
        data_root=Path(store.get("data_root", "/home/aiworker-1/spacebio-data")),
        flush_interval_s=float(store.get("flush_interval_s", 1.0)),
        flush_record_count=int(store.get("flush_record_count", 100)),
        state_root=Path(
            coordinator.get("state_root", "/home/aiworker-1/clinostat/spacebio-state")
        ),
    )


__all__ = ["GatewayConfig", "load_config"]
