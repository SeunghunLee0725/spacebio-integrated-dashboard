"""S1 — pump serial backend verified against a fake firmware emulator.

No hardware needed: FakeFirmware implements the pump_serial.md protocol in
memory, so we assert the exact bytes SerialPumpBackend puts on the wire and
that it parses responses / raises on ERR and timeout.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.pump_serial import SerialPumpBackend, PumpSerialError  # noqa: E402
from gateway.pump_actuator import PumpActuator, Limits  # noqa: E402


class FakeFirmware:
    """Minimal in-memory emulation of pump_driver.ino's line protocol."""

    def __init__(self):
        self.received: list[str] = []
        self._out: bytes = b""

    # -- pyserial-like surface --
    def reset_input_buffer(self):
        self._out = b""

    def flush(self):
        pass

    def close(self):
        pass

    def write(self, data: bytes):
        line = data.decode("ascii").strip()
        self.received.append(line)
        self._out = (self._respond(line) + "\n").encode("ascii")

    def readline(self) -> bytes:
        out, self._out = self._out, b""
        return out  # b"" -> caller sees a timeout

    def _respond(self, line: str) -> str:
        parts = line.split()
        cmd = parts[0] if parts else ""
        if cmd == "PING":
            return "PONG"
        if cmd == "STOP":
            return "OK STOP"
        if cmd == "RUN":
            return f"OK RUN {parts[1]}"
        if cmd == "DISP":
            return f"OK DISP {parts[1]} {parts[2]}"
        if cmd == "SEL":
            return f"OK SEL {parts[1]}"
        return "ERR unknown"


def _backend():
    fw = FakeFirmware()
    return SerialPumpBackend("fake", serial_obj=fw), fw


def test_ping():
    b, fw = _backend()
    assert b.ping() is True
    assert fw.received == ["PING"]


def test_run_stop_wire_format():
    b, fw = _backend()
    b.run(50.0)
    b.stop()
    assert fw.received == ["RUN 50.00", "STOP"]


def test_dispense_and_select_wire_format():
    b, fw = _backend()
    b.select_source("media")
    b.dispense(200.0, 50.0)
    assert fw.received == ["SEL media", "DISP 200.00 50.00"]


def test_select_none_when_no_valve():
    b, fw = _backend()
    b.select_source(None)
    assert fw.received == ["SEL none"]


def test_err_response_raises():
    b, _ = _backend()
    with pytest.raises(PumpSerialError):
        b._txn("BOGUS")


def test_timeout_raises():
    class Silent(FakeFirmware):
        def write(self, data: bytes):
            self.received.append(data.decode().strip())  # no response queued

    b = SerialPumpBackend("fake", serial_obj=Silent())
    with pytest.raises(PumpSerialError):
        b.stop()


def test_actuator_over_serial_clamps_then_dispenses():
    fw = FakeFirmware()
    backend = SerialPumpBackend("fake", serial_obj=fw)
    pump = PumpActuator(backend, Limits(max_volume_ul=100.0, max_rate_ul_s=60.0))
    dispensed = pump.dispense(volume_ul=500.0, rate_ul_s=999.0, source="drug")
    assert dispensed == 100.0                       # host clamp applied
    assert fw.received == ["SEL drug", "DISP 100.00 60.00"]  # clamped values on wire
