"""Topic constants and payload schemas shared across the gateway.

Mirror of contracts/topics.md — keep the two in sync. All payloads are
UTF-8 JSON. Dataclasses are frozen (immutable) per project style.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

ROOT = "s25007"


class Topics:
    GATEWAY_STATUS = f"{ROOT}/gateway/status"
    CLINOSTAT_STATUS = f"{ROOT}/clinostat/status"
    CLINOSTAT_CMD = f"{ROOT}/clinostat/cmd"
    LOC_SENSOR = f"{ROOT}/loc/sensor"
    PUMP_STATUS = f"{ROOT}/pump/status"
    PUMP_CMD = f"{ROOT}/pump/cmd"
    LOOP_STATE = f"{ROOT}/loop/state"
    LOOP_CMD = f"{ROOT}/loop/cmd"


def now_ms() -> int:
    return int(time.time() * 1000)


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode(raw: bytes) -> dict[str, Any]:
    """Parse an incoming JSON payload. Raises ValueError on malformed input."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed payload: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("payload must be a JSON object")
    return obj


@dataclass(frozen=True)
class GatewayStatus:
    state: str = "online"          # online | degraded | offline
    ble: str = "disconnected"      # connected | disconnected | scanning
    pump: str = "idle"             # idle | running | fault
    loop: str = "disarmed"         # armed | disarmed
    src: str = "gateway"

    def to_payload(self) -> dict[str, Any]:
        return {"ts": now_ms(), **asdict(self)}


@dataclass(frozen=True)
class SensorSample:
    tension_n: float
    delta_r_over_r0: float
    temp_c: float
    battery_pct: Optional[int] = None
    src: str = "loc"

    def to_payload(self) -> dict[str, Any]:
        return {"ts": now_ms(), **asdict(self)}


@dataclass(frozen=True)
class PumpStatus:
    state: str = "idle"            # idle | running | fault
    action: Optional[str] = None   # on | off | dispense | stop
    source: Optional[str] = None   # media | drug | None
    dispensed_ul: Optional[float] = None
    rate_ul_s: Optional[float] = None
    src: str = "pump"

    def to_payload(self) -> dict[str, Any]:
        return {"ts": now_ms(), **asdict(self)}


@dataclass(frozen=True)
class PumpCommand:
    action: str                    # on | off | dispense | stop
    source: Optional[str] = None   # media | drug | None (no valve)
    volume_ul: Optional[float] = None
    rate_ul_s: Optional[float] = None

    @staticmethod
    def from_payload(obj: dict[str, Any]) -> "PumpCommand":
        action = obj.get("action")
        if action not in ("on", "off", "dispense", "stop"):
            raise ValueError(f"invalid pump action: {action!r}")
        return PumpCommand(
            action=action,
            source=obj.get("source"),
            volume_ul=obj.get("volume_ul"),
            rate_ul_s=obj.get("rate_ul_s"),
        )


@dataclass(frozen=True)
class LoopCommand:
    action: str                    # arm | disarm | estop
    trigger: Optional[str] = None  # e.g. tension_n
    threshold: Optional[float] = None
    on_trigger: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_payload(obj: dict[str, Any]) -> "LoopCommand":
        action = obj.get("action")
        if action not in ("arm", "disarm", "estop"):
            raise ValueError(f"invalid loop action: {action!r}")
        return LoopCommand(
            action=action,
            trigger=obj.get("trigger"),
            threshold=obj.get("threshold"),
            on_trigger=obj.get("on_trigger") or {},
        )
