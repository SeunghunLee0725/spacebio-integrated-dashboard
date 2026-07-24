"""Closed-loop controller (edge, local) — DEC-008.

Skeleton rule engine: when an armed trigger crosses its threshold, run the
configured dispense action once, with a refractory period and interlocks.
The tight loop lives here, not in the app or the cloud.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Optional

from gateway.contracts import SensorSample
from gateway.pump_actuator import PumpActuator

logger = logging.getLogger("gateway.loop")


@dataclass(frozen=True)
class LoopState:
    armed: bool = False
    trigger: Optional[str] = None
    threshold: Optional[float] = None
    last_fired_ts: Optional[int] = None
    interlock: str = "ok"          # ok | max_volume | max_duration | watchdog
    on_trigger: dict[str, Any] = None  # type: ignore[assignment]

    def to_payload(self) -> dict[str, Any]:
        return {
            "ts": int(time.time() * 1000), "src": "loop",
            "armed": self.armed, "trigger": self.trigger,
            "threshold": self.threshold, "last_fired_ts": self.last_fired_ts,
            "interlock": self.interlock,
        }


class ControlLoop:
    def __init__(self, pump: PumpActuator, refractory_s: float = 30.0) -> None:
        self._pump = pump
        self._refractory_s = refractory_s
        self._state = LoopState()

    @property
    def state(self) -> LoopState:
        return self._state

    def arm(self, trigger: str, threshold: float, on_trigger: dict[str, Any]) -> LoopState:
        self._state = replace(self._state, armed=True, trigger=trigger,
                              threshold=threshold, on_trigger=on_trigger,
                              interlock="ok")
        logger.info("loop armed: %s >= %s -> %s", trigger, threshold, on_trigger)
        return self._state

    def disarm(self) -> LoopState:
        self._state = replace(self._state, armed=False)
        logger.info("loop disarmed")
        return self._state

    def estop(self) -> LoopState:
        self._pump.stop()
        self._state = replace(self._state, armed=False, interlock="watchdog")
        logger.warning("E-STOP: pump halted, loop disarmed")
        return self._state

    def on_sample(self, sample: SensorSample) -> Optional[LoopState]:
        """Evaluate a sample. Returns a new state if it changed, else None."""
        s = self._state
        if not s.armed or s.trigger is None or s.threshold is None:
            return None
        value = getattr(sample, s.trigger, None)
        if value is None or value < s.threshold:
            return None
        now = int(time.time() * 1000)
        if s.last_fired_ts is not None and (now - s.last_fired_ts) < self._refractory_s * 1000:
            return None
        self._fire(s.on_trigger or {})
        self._state = replace(s, last_fired_ts=now)
        return self._state

    def _fire(self, action: dict[str, Any]) -> None:
        volume = float(action.get("volume_ul", 0.0))
        rate = float(action.get("rate_ul_s", 50.0))
        source = action.get("source")
        logger.info("TRIGGER fired -> dispense %s ul (%s)", volume, source)
        self._pump.dispense(volume, rate, source=source)
