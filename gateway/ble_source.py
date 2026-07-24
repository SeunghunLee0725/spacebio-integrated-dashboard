"""BLE sensor source (interface + simulator).

The real backend wraps the existing bleak client in
LOC_circuit_design/software/ble_receiver. Until a board is connected,
SimulatedSource emits synthetic samples so the full pipeline (bridge,
store, loop) can be exercised on the laptop.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import AsyncIterator

from gateway.contracts import SensorSample

logger = logging.getLogger("gateway.ble")


class SimulatedSource:
    """Emits a slow tension ramp + noise-free sinusoid. For S0/S4 dry runs."""

    def __init__(self, rate_hz: float = 50.0) -> None:
        self._period = 1.0 / rate_hz

    async def stream(self) -> AsyncIterator[SensorSample]:
        t = 0.0
        while True:
            tension = 3.0 + 2.0 * (0.5 + 0.5 * math.sin(t * 0.2))
            yield SensorSample(
                tension_n=round(tension, 3),
                delta_r_over_r0=round(tension / 100.0, 4),
                temp_c=36.8,
                battery_pct=87,
            )
            t += self._period
            await asyncio.sleep(self._period)


# Real backend placeholder — to be wired in S4.
# class BleakSource:
#     """Wrap ble_receiver.BLEClient and yield SensorSample per notification."""
#     ...
