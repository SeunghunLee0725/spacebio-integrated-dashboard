"""센서 소스 테스트 — CSV 재생 + 합성 신호 (설계 스펙 6.3/6.7).

가짜 clock을 주입해 실제 sleep 없이 스케줄링을 검증한다.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from gateway.api_models import SensorSample
from gateway.csv_replay import (
    CsvReplaySource,
    load_csv_replay_source,
    load_manifest,
    parse_csv_rows,
    resolve_dataset_path,
)
from gateway.sensor_source import SensorSourceError
from gateway.synthetic_sensor import SyntheticSensorSource

FIXTURES = Path(__file__).parent / "fixtures"
REAL_DATASETS_DIR = Path(__file__).parent.parent / "datasets"
REAL_MANIFEST_PATH = REAL_DATASETS_DIR / "manifest.json"


class FakeClock:
    """`time.monotonic` 대체 — 테스트가 시각을 직접 제어한다."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# ─────────────────────────── CSV 파싱 ───────────────────────────

def test_parse_csv_rows_parses_required_columns():
    rows = parse_csv_rows(FIXTURES / "resistance_valid.csv")
    assert len(rows) == 4
    assert rows[0].timestamp_ms == 1000
    assert rows[0].raw_adc == 1200
    assert rows[0].resistance_ohm == 416.43
    assert rows[0].battery_pct == 90


def test_parse_csv_rows_accepts_optional_elapsed_column():
    rows = parse_csv_rows(FIXTURES / "resistance_optional_elapsed.csv")
    assert len(rows) == 3
    assert rows[0].timestamp_ms == 505688


def test_parse_csv_rows_allows_duplicate_timestamps_preserving_order():
    rows = parse_csv_rows(FIXTURES / "resistance_valid.csv")
    assert [r.timestamp_ms for r in rows] == [1000, 1100, 1100, 1300]
    assert rows[1].raw_adc == 1210
    assert rows[2].raw_adc == 1215


def test_parse_csv_rows_rejects_whole_file_on_decreasing_timestamp():
    with pytest.raises(SensorSourceError):
        parse_csv_rows(FIXTURES / "resistance_invalid.csv")


def test_parse_csv_rows_rejects_missing_required_column(tmp_path):
    bad = tmp_path / "missing_column.csv"
    bad.write_text(
        "timestamp_ms,raw_adc,resistance_ohm,delta_r_over_r0,temperature_c\n"
        "1000,1200,416.43,0.0,25.0\n"
    )
    with pytest.raises(SensorSourceError):
        parse_csv_rows(bad)


def test_parse_csv_rows_rejects_unknown_column(tmp_path):
    bad = tmp_path / "extra_column.csv"
    bad.write_text(
        "timestamp_ms,raw_adc,resistance_ohm,delta_r_over_r0,temperature_c,"
        "battery_pct,mystery\n"
        "1000,1200,416.43,0.0,25.0,90,???\n"
    )
    with pytest.raises(SensorSourceError):
        parse_csv_rows(bad)


def test_parse_csv_rows_rejects_non_finite_value(tmp_path):
    bad = tmp_path / "nan_value.csv"
    bad.write_text(
        "timestamp_ms,raw_adc,resistance_ohm,delta_r_over_r0,temperature_c,battery_pct\n"
        "1000,1200,nan,0.0,25.0,90\n"
    )
    with pytest.raises(SensorSourceError):
        parse_csv_rows(bad)


def test_parse_csv_rows_rejects_out_of_range_battery_pct(tmp_path):
    bad = tmp_path / "bad_battery.csv"
    bad.write_text(
        "timestamp_ms,raw_adc,resistance_ohm,delta_r_over_r0,temperature_c,battery_pct\n"
        "1000,1200,416.43,0.0,25.0,101\n"
    )
    with pytest.raises(SensorSourceError):
        parse_csv_rows(bad)


def test_parse_csv_rows_ignores_blank_lines(tmp_path):
    bad = tmp_path / "with_blank.csv"
    bad.write_text(
        "timestamp_ms,raw_adc,resistance_ohm,delta_r_over_r0,temperature_c,battery_pct\n"
        "1000,1200,416.43,0.0,25.0,90\n"
        "\n"
        "1100,1210,420.11,0.0088,25.1,90\n"
    )
    rows = parse_csv_rows(bad)
    assert len(rows) == 2


# ─────────────────────────── dataset 허용목록 ───────────────────────────

def test_load_manifest_reads_registered_dataset():
    manifest = load_manifest(REAL_MANIFEST_PATH)
    assert "thinkpad_20260714_172138_ble_test" in manifest
    entry = manifest["thinkpad_20260714_172138_ble_test"]
    assert entry.sample_count == 522


def test_resolve_dataset_path_rejects_unknown_dataset_id():
    manifest = load_manifest(REAL_MANIFEST_PATH)
    with pytest.raises(SensorSourceError):
        resolve_dataset_path(
            "not_registered", datasets_dir=REAL_DATASETS_DIR, manifest=manifest
        )


def test_resolve_dataset_path_rejects_path_traversal(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "datasets": [{
            "dataset_id": "evil",
            "filename": "../evil.csv",
            "sha256": "0" * 64,
            "provenance": "test",
            "sample_count": 1,
        }]
    }))
    (tmp_path / "evil.csv").write_text("not real data")
    datasets_dir = tmp_path / "datasets"
    datasets_dir.mkdir()
    manifest = load_manifest(manifest_path)
    with pytest.raises(SensorSourceError):
        resolve_dataset_path("evil", datasets_dir=datasets_dir, manifest=manifest)


def test_resolve_dataset_path_rejects_sha256_mismatch(tmp_path):
    datasets_dir = tmp_path / "datasets"
    datasets_dir.mkdir()
    (datasets_dir / "data.csv").write_text("mismatched content")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "datasets": [{
            "dataset_id": "data",
            "filename": "data.csv",
            "sha256": "0" * 64,
            "provenance": "test",
            "sample_count": 1,
        }]
    }))
    manifest = load_manifest(manifest_path)
    with pytest.raises(SensorSourceError):
        resolve_dataset_path("data", datasets_dir=datasets_dir, manifest=manifest)


def test_load_csv_replay_source_loads_registered_dataset():
    source = load_csv_replay_source(
        "thinkpad_20260714_172138_ble_test",
        datasets_dir=REAL_DATASETS_DIR,
        manifest_path=REAL_MANIFEST_PATH,
        clock=FakeClock(),
    )
    assert isinstance(source, CsvReplaySource)


def test_real_dataset_replays_all_522_rows_as_valid_sensor_samples():
    """실측 raw_adc는 ADS1115 16비트 차동이라 음수를 포함한다(-13263~12985).

    api_models.RawAdc가 이 범위를 반영하지 못하면 SensorSample 생성이 즉시
    ValidationError로 터진다 — 합성 fixture(양수 소값)만으로는 이 문제를
    잡지 못하므로 실측 522행 전체를 replay로 관통시켜 회귀를 막는다.
    """
    clock = FakeClock()
    source = load_csv_replay_source(
        "thinkpad_20260714_172138_ble_test",
        datasets_dir=REAL_DATASETS_DIR,
        manifest_path=REAL_MANIFEST_PATH,
        loop=False,
        clock=clock,
    )
    source.start()
    clock.advance(1_000.0)  # dataset span is ~173s; jump past all of it at once

    samples: list[SensorSample] = []
    while True:
        sample = source.tick()
        if sample is None:
            break
        assert isinstance(sample, SensorSample)
        samples.append(sample)

    assert len(samples) == 522
    assert source.stopped is True
    assert samples[0].source_timestamp_ms == 505688
    assert samples[0].session_elapsed_ms == 0
    assert any(sample.raw_adc < 0 for sample in samples)  # ADS1115 음수 확인


# ─────────────────────────── 재생 스케줄링 ───────────────────────────

def _source(rows_fixture: str = "resistance_valid.csv", **kwargs) -> tuple[CsvReplaySource, FakeClock]:
    rows = parse_csv_rows(FIXTURES / rows_fixture)
    clock = FakeClock()
    source = CsvReplaySource(rows, clock=clock, **kwargs)
    return source, clock


def test_replay_start_is_deterministic_and_begins_at_index_zero():
    source, clock = _source(loop=False)
    source.start()
    sample = source.tick()
    assert isinstance(sample, SensorSample)
    assert sample.source_timestamp_ms == 1000
    assert sample.session_elapsed_ms == 0
    assert sample.loop_count == 0


def test_replay_returns_none_before_next_sample_is_due():
    source, clock = _source(loop=False)
    source.start()
    source.tick()  # index 0 @ t=0
    assert source.tick() is None  # next row due at rebased 100ms, clock hasn't moved


def test_replay_emits_samples_in_order_as_virtual_time_advances():
    source, clock = _source(loop=False)
    source.start()
    first = source.tick()
    clock.advance(0.1)  # 100ms -> row1 (rebased 100ms) due
    second = source.tick()
    clock.advance(0.2)  # +200ms = 300ms real -> row3 (rebased 300ms) due (skips dup ts row2 too? no: one per tick)
    third = source.tick()
    fourth = source.tick()

    assert [first.raw_adc, second.raw_adc, third.raw_adc, fourth.raw_adc] == [
        1200, 1210, 1215, 1220,
    ]
    assert third.source_timestamp_ms == 1100
    assert fourth.source_timestamp_ms == 1300
    assert fourth.session_elapsed_ms == 300


def test_replay_speed_scales_schedule():
    source, clock = _source(loop=False, replay_speed=10.0)
    source.start()
    source.tick()  # index0
    clock.advance(0.01)  # 10ms real * 10x speed = 100ms virtual -> row1 due
    second = source.tick()
    assert second.raw_adc == 1210


def test_replay_loop_wraps_to_index_zero_and_increments_loop_count():
    source, clock = _source(loop=True)
    source.start()
    last = None
    for _ in range(4):  # drains the 4-row dataset, including the EOF row
        last = source.tick()
        clock.advance(1.0)  # plenty of time to reach next scheduled sample
    assert last.source_timestamp_ms == 1300
    assert last.loop_count == 0  # last row of the first pass
    assert source.loop_count == 1  # wrap already applied internally at EOF

    wrapped = source.tick()
    assert wrapped.source_timestamp_ms == 1000
    assert wrapped.session_elapsed_ms == 0
    assert wrapped.loop_count == 1


def test_replay_stops_at_eof_when_loop_false():
    source, clock = _source(loop=False)
    source.start()
    last = None
    for _ in range(4):
        last = source.tick()
        clock.advance(1.0)
    assert last.source_timestamp_ms == 1300
    assert source.stopped is True
    assert source.tick() is None


def test_replay_restart_is_deterministic():
    source, clock = _source(loop=False)
    source.start()
    source.tick()
    clock.advance(5.0)
    source.tick()
    source.start()  # restart mid-stream
    clock.advance(0.0)
    restarted = source.tick()
    assert restarted.source_timestamp_ms == 1000
    assert restarted.loop_count == 0


# ─────────────────────────── 합성 신호 ───────────────────────────

def _synthetic(**overrides) -> SyntheticSensorSource:
    params = dict(
        baseline_resistance_ohm=1000.0,
        amplitude_ohm=100.0,
        period_s=10.0,
        noise_std_ohm=5.0,
        seed=42,
        temperature_c=30.0,
        battery_pct=77,
    )
    params.update(overrides)
    return SyntheticSensorSource(**params)


def test_synthetic_sample_matches_sine_plus_noise_formula():
    source = _synthetic(noise_std_ohm=0.0)  # noise off -> exact sine check
    sample = source.sample_at_index(0)
    assert sample.resistance_ohm == pytest.approx(1000.0)  # sin(0) == 0

    interval_s = 0.1  # default sample_interval_ms == 100ms
    index = 25  # t = 2.5s
    t = index * interval_s
    expected_r = 1000.0 + 100.0 * math.sin(2 * math.pi * t / 10.0)
    sample = source.sample_at_index(index)
    assert sample.resistance_ohm == pytest.approx(expected_r)


def test_synthetic_delta_r_over_r0_matches_formula():
    source = _synthetic(noise_std_ohm=0.0)
    sample = source.sample_at_index(10)
    expected_delta = (sample.resistance_ohm - 1000.0) / 1000.0
    assert sample.delta_r_over_r0 == pytest.approx(expected_delta)


def test_synthetic_fixed_temperature_and_battery():
    source = _synthetic()
    for index in (0, 5, 50):
        sample = source.sample_at_index(index)
        assert sample.temperature_c == 30.0
        assert sample.battery_pct == 77


def test_synthetic_raw_adc_uses_divider_inverse_and_clamps_to_full_scale():
    source = _synthetic(noise_std_ohm=0.0)
    sample = source.sample_at_index(0)
    expected_ratio = 1000.0 / (1000.0 + 82_500.0)
    expected_raw_adc = round(expected_ratio * 4095)
    assert sample.raw_adc == expected_raw_adc
    assert 0 <= sample.raw_adc <= 4095


def test_synthetic_raw_adc_respects_injected_reference_and_full_scale():
    source = _synthetic(
        noise_std_ohm=0.0, reference_resistor_ohm=1000.0, adc_full_scale=1023,
    )
    sample = source.sample_at_index(0)  # R == baseline == reference -> ratio 0.5
    assert sample.raw_adc == round(0.5 * 1023)


def test_synthetic_same_seed_and_index_are_byte_identical():
    a = _synthetic(seed=7).sample_at_index(123)
    b = _synthetic(seed=7).sample_at_index(123)
    assert a == b


def test_synthetic_different_seed_changes_noise():
    a = _synthetic(seed=1).sample_at_index(3)
    b = _synthetic(seed=2).sample_at_index(3)
    assert a.resistance_ohm != b.resistance_ohm


def test_synthetic_index_order_does_not_affect_value():
    """샘플 값은 index만의 순수 함수 — 생성 순서에 의존하지 않는다."""
    source = _synthetic(seed=99)
    out_of_order = source.sample_at_index(50)
    source_again = _synthetic(seed=99)
    for i in range(51):
        source_again.sample_at_index(i)
    in_order = source_again.sample_at_index(50)
    assert out_of_order == in_order


def test_synthetic_uses_sensor_sample_schema():
    source = _synthetic()
    sample = source.sample_at_index(0)
    assert isinstance(sample, SensorSample)
    assert sample.loop_count == 0


def test_synthetic_tick_uses_injected_clock_without_sleeping():
    clock = FakeClock()
    source = _synthetic(clock=clock)
    source.start()
    first = source.tick()
    assert first is not None
    assert first == source.sample_at_index(0)
    assert source.tick() is None  # not due yet at same clock value

    clock.advance(0.1)  # one sample_interval_ms (100ms) later
    second = source.tick()
    assert second == source.sample_at_index(1)


def test_synthetic_restart_is_deterministic():
    clock = FakeClock()
    source = _synthetic(clock=clock, seed=5)
    source.start()
    first = source.tick()
    clock.advance(1.0)
    source.tick()
    source.start()
    restarted = source.tick()
    assert restarted == first
