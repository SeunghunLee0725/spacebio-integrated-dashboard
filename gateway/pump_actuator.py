"""Pump + optional valve as a single 'actuator sequence' abstraction.

The fluid path decision (valve / bolus / manual) is undecided
(DEC-009), so this exposes a source-agnostic API: select() -> dispense()
-> restore(). Backends implement how a 'source' is actually selected.

`dispense` is an atomic, bounded action: the backend (pump firmware) runs
for the computed time and auto-stops. This keeps a host crash mid-dispense
from leaving the pump running (defense-in-depth with the firmware watchdog
and firmware hard limits — DEC-008).

Hard limits (max volume / rate) are clamped here on top of the firmware
limits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

logger = logging.getLogger("gateway.pump")


@dataclass(frozen=True)
class Limits:
    max_volume_ul: float = 1000.0
    max_rate_ul_s: float = 200.0


class PumpBackend(Protocol):
    """Hardware driver. The serial backend implements these against the board."""

    def run(self, rate_ul_s: float) -> None: ...
    def stop(self) -> None: ...
    def select_source(self, source: Optional[str]) -> None: ...
    def dispense(self, volume_ul: float, rate_ul_s: float) -> None: ...


class NullBackend:
    """No hardware attached — logs actions. Used until a pump is wired."""

    def run(self, rate_ul_s: float) -> None:
        logger.info("[null-pump] run rate=%.1f ul/s", rate_ul_s)

    def stop(self) -> None:
        logger.info("[null-pump] stop")

    def select_source(self, source: Optional[str]) -> None:
        logger.info("[null-pump] select source=%s", source)

    def dispense(self, volume_ul: float, rate_ul_s: float) -> None:
        duration_s = volume_ul / rate_ul_s if rate_ul_s > 0 else 0.0
        logger.info("[null-pump] dispense %.1f ul @ %.1f ul/s (~%.1fs)",
                    volume_ul, rate_ul_s, duration_s)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class PumpActuator:
    def __init__(self, backend: Optional[PumpBackend] = None,
                 limits: Optional[Limits] = None) -> None:
        self._backend = backend or NullBackend()
        self._limits = limits or Limits()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def on(self, rate_ul_s: float) -> None:
        rate = _clamp(rate_ul_s, 0.0, self._limits.max_rate_ul_s)
        self._backend.run(rate)
        self._running = True

    def off(self) -> None:
        self._backend.stop()
        self._running = False

    def dispense(self, volume_ul: float, rate_ul_s: float,
                 source: Optional[str] = None) -> float:
        """select source -> pump a clamped volume (backend-timed) -> auto-stop.

        Returns the clamped volume so the loop/status can report it.
        """
        volume = _clamp(volume_ul, 0.0, self._limits.max_volume_ul)
        rate = _clamp(rate_ul_s, 1.0, self._limits.max_rate_ul_s)
        if volume < volume_ul or rate < rate_ul_s:
            logger.warning("dispense clamped: vol %.1f->%.1f rate %.1f->%.1f",
                           volume_ul, volume, rate_ul_s, rate)
        self._backend.select_source(source)
        self._backend.dispense(volume, rate)
        return volume

    def stop(self) -> None:
        self.off()
