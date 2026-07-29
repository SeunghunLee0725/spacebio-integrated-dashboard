"""실기 저항센서 시리얼 소스 — 가짜 포트로 검증(실제 /dev/ttyACM0 불필요)."""

from __future__ import annotations

from gateway.serial_sensor import SerialSensorSource, parse_data_line


LINE = "[Data] t=15821367ms  R=1000000.0Ω  ΔR/R0=3.100%  T=25.4°C  Bat=87%  BLE=connected"


def test_parse_data_line_reads_all_fields():
    p = parse_data_line(LINE)
    assert p["timestamp_ms"] == 15821367
    assert p["resistance_ohm"] == 1_000_000.0
    assert abs(p["delta_r_over_r0"] - 0.031) < 1e-9   # 백분율 → 비율
    assert p["temperature_c"] == 25.4
    assert p["battery_pct"] == 87


def test_parse_data_line_ignores_non_data_lines():
    assert parse_data_line("boot complete") is None
    assert parse_data_line("") is None


class FakePort:
    """readline이 미리 넣은 줄을 순서대로 내는 가짜 시리얼 포트."""

    def __init__(self, lines):
        self._lines = [l.encode() for l in lines]

    def reset_input_buffer(self): ...
    def close(self): ...

    def readline(self):
        return self._lines.pop(0) if self._lines else b""


def test_source_yields_sample_and_rebases_elapsed():
    port = FakePort([
        LINE + "\n",
        "[Data] t=15821867ms  R=999000.0Ω  ΔR/R0=-0.1%  T=25.0°C  Bat=86%  BLE=connected\n",
    ])
    src = SerialSensorSource(serial_factory=lambda: port)
    src.start()

    first = src.tick()
    assert first is not None
    assert first.raw_adc is None                 # 실기 형식엔 raw ADC가 없다
    assert first.session_elapsed_ms == 0         # 첫 샘플 경과 0으로 rebase
    assert first.resistance_ohm == 1_000_000.0

    second = src.tick()
    assert second.session_elapsed_ms == 500      # 15821867 - 15821367


def test_source_returns_none_when_no_new_line():
    src = SerialSensorSource(serial_factory=lambda: FakePort([]))
    src.start()
    assert src.tick() is None
