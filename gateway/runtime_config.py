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
    #: 실기 무선 펌프(pump_backend=="wireless")용 MQTT 브로커.
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: Optional[str] = None
    mqtt_password: Optional[str] = None
    #: 실기 저항센서(SERIAL_LIVE)용 시리얼 포트.
    #: ⚠ `/dev/ttyACM*` 번호는 연결 순서에 따라 바뀐다(2026-07-29에 펌프 보드가
    #: ttyACM0을 차지한 적이 있다). `/dev/serial/by-id/...` 고정 경로를 권장한다.
    sensor_serial_port: str = "/dev/ttyACM0"
    sensor_serial_baudrate: int = 115200
    #: 실기 저항센서(BLE_LIVE) 탐색값. 기본은 서비스 UUID로 찾고, 이름은 보조 필터다
    #: — 이 센서는 광고에 Local Name을 넣지 않는다(2026-07-29 실기 확인).
    #: 센서를 여러 대 붙일 때만 `ble.address`로 특정한다.
    ble_device_name: Optional[str] = None
    ble_address: Optional[str] = None
    #: 레퍼런스 저항(Ω). 장치에 써 넣는 설정이며 펌웨어가 저항을 재계산한다.
    #: None 이면 보드의 기존 설정을 그대로 둔다.
    ble_rref_ohm: Optional[float] = None
    #: 시간평균 계수 — 원시 N개를 하나로 평균한다(출력 주파수 = fs/N).
    ble_avg_factor: int = 1
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
    mqtt = raw.get("mqtt") or {}
    ble = raw.get("ble") or {}
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
        mqtt_host=str(mqtt.get("host", "127.0.0.1")),
        mqtt_port=int(mqtt.get("port", 1883)),
        mqtt_username=mqtt.get("username"),
        mqtt_password=mqtt.get("password"),
        sensor_serial_port=str(sensor.get("serial_port", "/dev/ttyACM0")),
        sensor_serial_baudrate=int(sensor.get("serial_baudrate", 115200)),
        ble_device_name=ble.get("device_name") or None,
        ble_address=ble.get("address") or None,
        ble_rref_ohm=(float(ble["rref_ohm"]) if ble.get("rref_ohm") is not None else None),
        ble_avg_factor=int(ble.get("avg_factor", 1)),
        data_root=Path(store.get("data_root", "/home/aiworker-1/spacebio-data")),
        flush_interval_s=float(store.get("flush_interval_s", 1.0)),
        flush_record_count=int(store.get("flush_record_count", 100)),
        state_root=Path(
            coordinator.get("state_root", "/home/aiworker-1/clinostat/spacebio-state")
        ),
    )


__all__ = ["GatewayConfig", "load_config"]
