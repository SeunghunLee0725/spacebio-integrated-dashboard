"""Serial backend for the pump driver board (pyserial).

Implements the PumpBackend protocol over the ASCII line protocol defined in
contracts/pump_serial.md. Newline-terminated commands, one-line responses.
All I/O is guarded; a timeout or serial error is logged and surfaced as
PumpSerialError rather than crashing the gateway.
"""

from __future__ import annotations

import logging
from typing import Optional

import serial  # pyserial

logger = logging.getLogger("gateway.pump.serial")


class PumpSerialError(RuntimeError):
    pass


class SerialPumpBackend:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0,
                 serial_obj=None) -> None:
        # serial_obj lets tests inject a fake firmware without real hardware.
        if serial_obj is not None:
            self._ser = serial_obj
            self._port = port
            return
        try:
            self._ser = serial.Serial(port, baudrate=baud, timeout=timeout)
        except serial.SerialException as exc:
            raise PumpSerialError(f"cannot open pump port {port}: {exc}") from exc
        self._port = port

    def _txn(self, command: str) -> str:
        """Send one command line, return the board's one-line response (stripped)."""
        line = (command.strip() + "\n").encode("ascii")
        try:
            self._ser.reset_input_buffer()
            self._ser.write(line)
            self._ser.flush()
            resp = self._ser.readline().decode("ascii", "replace").strip()
        except serial.SerialException as exc:
            raise PumpSerialError(f"serial I/O failed on {command!r}: {exc}") from exc
        if not resp:
            raise PumpSerialError(f"no response to {command!r} (timeout)")
        if resp.startswith("ERR"):
            raise PumpSerialError(f"board rejected {command!r}: {resp}")
        logger.debug("pump txn %r -> %r", command, resp)
        return resp

    # -- PumpBackend protocol -----------------------------------------
    def run(self, rate_ul_s: float) -> None:
        self._txn(f"RUN {rate_ul_s:.2f}")

    def stop(self) -> None:
        self._txn("STOP")

    def select_source(self, source: Optional[str]) -> None:
        self._txn(f"SEL {source or 'none'}")

    def dispense(self, volume_ul: float, rate_ul_s: float) -> None:
        # Board runs the computed time and auto-stops (fires async 'DONE DISP').
        self._txn(f"DISP {volume_ul:.2f} {rate_ul_s:.2f}")

    def ping(self) -> bool:
        try:
            return self._txn("PING") == "PONG"
        except PumpSerialError:
            return False

    def close(self) -> None:
        try:
            self._ser.close()
        except serial.SerialException:
            logger.warning("error closing pump port %s", self._port)
