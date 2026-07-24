"""Broker-independent tests: payload contracts, actuator clamping, loop rules."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.contracts import PumpCommand, LoopCommand, SensorSample, decode, encode  # noqa: E402
from gateway.pump_actuator import PumpActuator, Limits  # noqa: E402
from gateway.control_loop import ControlLoop  # noqa: E402


def test_encode_decode_roundtrip():
    payload = SensorSample(tension_n=3.1, delta_r_over_r0=0.031, temp_c=36.8).to_payload()
    assert decode(encode(payload))["tension_n"] == 3.1


def test_decode_rejects_malformed():
    with pytest.raises(ValueError):
        decode(b"not json")


def test_pump_command_validates_action():
    with pytest.raises(ValueError):
        PumpCommand.from_payload({"action": "explode"})
    assert PumpCommand.from_payload({"action": "dispense", "volume_ul": 50}).volume_ul == 50


def test_loop_command_validates_action():
    with pytest.raises(ValueError):
        LoopCommand.from_payload({"action": "nope"})
    assert LoopCommand.from_payload({"action": "arm"}).action == "arm"


def test_actuator_clamps_to_limits():
    pump = PumpActuator(limits=Limits(max_volume_ul=100.0, max_rate_ul_s=60.0))
    dispensed = pump.dispense(volume_ul=500.0, rate_ul_s=999.0, source="drug")
    assert dispensed == 100.0  # clamped to max_volume_ul


def test_loop_fires_only_when_armed_and_over_threshold():
    pump = PumpActuator()
    loop = ControlLoop(pump, refractory_s=0.0)
    low = SensorSample(tension_n=1.0, delta_r_over_r0=0.01, temp_c=36.0)
    high = SensorSample(tension_n=9.0, delta_r_over_r0=0.09, temp_c=36.0)

    assert loop.on_sample(high) is None  # disarmed -> no fire
    loop.arm("tension_n", 5.0, {"source": "drug", "volume_ul": 50})
    assert loop.on_sample(low) is None   # below threshold
    state = loop.on_sample(high)         # crosses -> fires
    assert state is not None and state.last_fired_ts is not None
