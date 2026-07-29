"""API 계약 모델 테스트 — 설계 스펙 6장.

범위 밖 값은 clamp하지 않고 거부한다. 변경 요청은 unknown field를 금지한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from gateway import api_models as m


# ─────────────────────────── 상태 enum ───────────────────────────

def test_state_enums_have_exactly_the_specified_members():
    assert {s.value for s in m.GatewayState} == {"online", "degraded"}
    assert {s.value for s in m.SensorState} == {"idle", "running", "stopped", "fault"}
    assert {s.value for s in m.PumpState} == {
        "idle", "running", "completed", "stopped", "fault", "emergency_stopped",
    }
    assert {s.value for s in m.SessionState} == {
        "idle", "preparing", "recording", "completed", "partial", "failed",
    }
    assert {s.value for s in m.SensorMode} == {
        "CSV_REPLAY", "SYNTHETIC", "SERIAL_LIVE", "BLE_LIVE",
    }
    assert m.PUMP_MODE == "SIMULATED"


# ─────────────────────────── 시각 ───────────────────────────

def test_timestamps_require_timezone():
    naive = datetime(2026, 7, 24, 10, 0, 0)
    with pytest.raises(ValidationError):
        m.SessionStartRequest(
            request_id="r1", session_id="spacebio_20260724_100000_ab12",
            experiment_name="smoke", started_at=naive,
        )


def test_timestamps_accept_offset_and_serialize_rfc3339():
    kst = timezone(timedelta(hours=9))
    req = m.SessionStartRequest(
        request_id="r1", session_id="spacebio_20260724_100000_ab12",
        experiment_name="smoke", started_at=datetime(2026, 7, 24, 10, 0, 0, tzinfo=kst),
    )
    assert req.started_at.utcoffset() == timedelta(hours=9)
    assert "2026-07-24T10:00:00+09:00" in req.model_dump_json()


# ─────────────────────────── 펌프 요청 범위 ───────────────────────────

@pytest.mark.parametrize("rate", [1.0, 50.0, 200.0])
def test_pump_dispense_accepts_rate_within_range(rate):
    assert m.PumpDispenseRequest(
        request_id="r1", rate_ul_s=rate, target_volume_ul=100.0
    ).rate_ul_s == rate


@pytest.mark.parametrize("rate", [0.0, 0.999, 200.001, 1000.0, -5.0])
def test_pump_dispense_rejects_rate_outside_range(rate):
    with pytest.raises(ValidationError):
        m.PumpDispenseRequest(request_id="r1", rate_ul_s=rate, target_volume_ul=100.0)


@pytest.mark.parametrize("volume", [1.0, 500.0, 1000.0])
def test_pump_dispense_accepts_volume_within_range(volume):
    assert m.PumpDispenseRequest(
        request_id="r1", rate_ul_s=50.0, target_volume_ul=volume
    ).target_volume_ul == volume


@pytest.mark.parametrize("volume", [0.0, 0.999, 1000.001, -1.0])
def test_pump_dispense_rejects_volume_outside_range(volume):
    with pytest.raises(ValidationError):
        m.PumpDispenseRequest(request_id="r1", rate_ul_s=50.0, target_volume_ul=volume)


def test_pump_dispense_rejects_rather_than_clamps():
    """범위 밖 입력이 조용히 경계값으로 바뀌면 안 된다."""
    with pytest.raises(ValidationError):
        m.PumpDispenseRequest(request_id="r1", rate_ul_s=250.0, target_volume_ul=100.0)


def test_pump_dispense_rejects_non_finite():
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            m.PumpDispenseRequest(request_id="r1", rate_ul_s=bad, target_volume_ul=100.0)


def test_pump_dispense_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        m.PumpDispenseRequest(
            request_id="r1", rate_ul_s=50.0, target_volume_ul=100.0, source="drug",
        )


def test_pump_quantities_round_to_three_decimals():
    req = m.PumpDispenseRequest(
        request_id="r1", rate_ul_s=50.123456, target_volume_ul=120.987654,
    )
    assert req.rate_ul_s == 50.123
    assert req.target_volume_ul == 120.988


# ─────────────────────────── 비상정지 ───────────────────────────

def test_estop_reset_requires_exact_acknowledgement():
    req = m.PumpResetEmergencyStopRequest(
        request_id="r1", acknowledgement=m.ESTOP_RESET_ACKNOWLEDGEMENT,
    )
    assert req.acknowledgement == "RESET_SIMULATED_PUMP_ESTOP"


@pytest.mark.parametrize("bad", [
    "reset_simulated_pump_estop",          # 대소문자 불일치
    " RESET_SIMULATED_PUMP_ESTOP",         # 앞 공백
    "RESET_SIMULATED_PUMP_ESTOP ",         # 뒤 공백
    "RESET_SIMULATED_PUMP_ESTOPX",
    "",
])
def test_estop_reset_rejects_inexact_acknowledgement(bad):
    with pytest.raises(ValidationError):
        m.PumpResetEmergencyStopRequest(request_id="r1", acknowledgement=bad)


# ─────────────────────────── 센서 설정 (CSV) ───────────────────────────

def test_csv_configure_accepts_valid_request():
    req = m.SensorConfigureCsvRequest(
        request_id="r1", mode="CSV_REPLAY",
        dataset_id="thinkpad_20260721_171750_ble_test", replay_speed=1.0, loop=True,
    )
    assert req.mode is m.SensorMode.CSV_REPLAY
    assert req.loop is True


@pytest.mark.parametrize("speed", [0.1, 1.0, 10.0])
def test_csv_configure_accepts_replay_speed_within_range(speed):
    assert m.SensorConfigureCsvRequest(
        request_id="r1", mode="CSV_REPLAY", dataset_id="d1", replay_speed=speed,
    ).replay_speed == speed


@pytest.mark.parametrize("speed", [0.0, 0.099, 10.001, -1.0])
def test_csv_configure_rejects_replay_speed_outside_range(speed):
    with pytest.raises(ValidationError):
        m.SensorConfigureCsvRequest(
            request_id="r1", mode="CSV_REPLAY", dataset_id="d1", replay_speed=speed,
        )


@pytest.mark.parametrize("dataset_id", [
    "../etc/passwd", "a/b", "a\\b", "/abs/path", "with space", "dot.dot", "",
])
def test_csv_configure_rejects_path_like_dataset_ids(dataset_id):
    """dataset_id는 allowlist 키지 경로가 아니다."""
    with pytest.raises(ValidationError):
        m.SensorConfigureCsvRequest(
            request_id="r1", mode="CSV_REPLAY", dataset_id=dataset_id,
        )


def test_csv_configure_forbids_synthetic_fields():
    with pytest.raises(ValidationError):
        m.SensorConfigureCsvRequest(
            request_id="r1", mode="CSV_REPLAY", dataset_id="d1", seed=25007,
        )


# ─────────────────────────── 센서 설정 (합성) ───────────────────────────

def _synthetic(**overrides):
    payload = dict(
        request_id="r1", mode="SYNTHETIC", baseline_resistance_ohm=82500.0,
        amplitude_ohm=500.0, period_s=30.0, noise_std_ohm=10.0, seed=25007,
        temperature_c=25.0, battery_pct=100,
    )
    payload.update(overrides)
    return m.SensorConfigureSyntheticRequest(**payload)


def test_synthetic_configure_accepts_valid_request():
    req = _synthetic()
    assert req.mode is m.SensorMode.SYNTHETIC
    assert req.seed == 25007


@pytest.mark.parametrize("baseline", [1_000.0, 82_500.0, 10_000_000.0])
def test_synthetic_accepts_baseline_within_range(baseline):
    assert _synthetic(
        baseline_resistance_ohm=baseline, amplitude_ohm=0.0, noise_std_ohm=0.0,
    ).baseline_resistance_ohm == baseline


@pytest.mark.parametrize("baseline", [999.9, 10_000_000.1, 0.0, -1.0])
def test_synthetic_rejects_baseline_outside_range(baseline):
    with pytest.raises(ValidationError):
        _synthetic(baseline_resistance_ohm=baseline, amplitude_ohm=0.0, noise_std_ohm=0.0)


def test_synthetic_amplitude_may_not_exceed_half_of_baseline():
    _synthetic(baseline_resistance_ohm=1000.0, amplitude_ohm=500.0, noise_std_ohm=0.0)
    with pytest.raises(ValidationError):
        _synthetic(baseline_resistance_ohm=1000.0, amplitude_ohm=500.1, noise_std_ohm=0.0)


def test_synthetic_noise_may_not_exceed_half_of_amplitude():
    _synthetic(amplitude_ohm=500.0, noise_std_ohm=250.0)
    with pytest.raises(ValidationError):
        _synthetic(amplitude_ohm=500.0, noise_std_ohm=250.1)


def test_synthetic_zero_amplitude_forces_zero_noise():
    _synthetic(amplitude_ohm=0.0, noise_std_ohm=0.0)
    with pytest.raises(ValidationError):
        _synthetic(amplitude_ohm=0.0, noise_std_ohm=0.1)


@pytest.mark.parametrize("period", [0.5, 30.0, 3600.0])
def test_synthetic_accepts_period_within_range(period):
    assert _synthetic(period_s=period).period_s == period


@pytest.mark.parametrize("period", [0.499, 3600.1, 0.0, -1.0])
def test_synthetic_rejects_period_outside_range(period):
    with pytest.raises(ValidationError):
        _synthetic(period_s=period)


@pytest.mark.parametrize("temp", [0.0, 25.0, 60.0])
def test_synthetic_accepts_temperature_within_range(temp):
    assert _synthetic(temperature_c=temp).temperature_c == temp


@pytest.mark.parametrize("temp", [-0.1, 60.1])
def test_synthetic_rejects_temperature_outside_range(temp):
    with pytest.raises(ValidationError):
        _synthetic(temperature_c=temp)


@pytest.mark.parametrize("battery", [-1, 101])
def test_synthetic_rejects_battery_outside_range(battery):
    with pytest.raises(ValidationError):
        _synthetic(battery_pct=battery)


def test_synthetic_forbids_csv_fields():
    with pytest.raises(ValidationError):
        _synthetic(dataset_id="d1")


# ─────────────────────────── 센서 설정 판별 ───────────────────────────

def test_configure_request_dispatches_on_mode():
    csv = m.parse_sensor_configure(
        {"request_id": "r1", "mode": "CSV_REPLAY", "dataset_id": "d1"}
    )
    assert isinstance(csv, m.SensorConfigureCsvRequest)
    synth = m.parse_sensor_configure({
        "request_id": "r1", "mode": "SYNTHETIC", "baseline_resistance_ohm": 82500.0,
        "amplitude_ohm": 500.0, "period_s": 30.0, "noise_std_ohm": 10.0, "seed": 1,
    })
    assert isinstance(synth, m.SensorConfigureSyntheticRequest)


def test_configure_request_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        m.parse_sensor_configure({"request_id": "r1", "mode": "LIVE_BLE"})


def test_ble_live_configure_defaults_device_name_to_none():
    """이름을 안 주면 None — 런타임이 config.yaml의 ble.device_name을 쓴다."""
    req = m.parse_sensor_configure({"request_id": "r1", "mode": "BLE_LIVE"})
    assert isinstance(req, m.SensorConfigureBleLiveRequest)
    assert req.device_name is None
    assert req.scan_timeout_s == 15.0


def test_ble_live_configure_accepts_overrides():
    req = m.parse_sensor_configure({
        "request_id": "r1", "mode": "BLE_LIVE",
        "device_name": "ResistanceSensor2", "scan_timeout_s": 30.0,
    })
    assert req.device_name == "ResistanceSensor2"
    assert req.scan_timeout_s == 30.0


@pytest.mark.parametrize("timeout", [0.0, -1.0, 121.0])
def test_ble_live_configure_rejects_bad_scan_timeout(timeout):
    with pytest.raises(ValidationError):
        m.parse_sensor_configure({
            "request_id": "r1", "mode": "BLE_LIVE", "scan_timeout_s": timeout,
        })


def test_ble_live_configure_rejects_empty_device_name():
    with pytest.raises(ValidationError):
        m.parse_sensor_configure({
            "request_id": "r1", "mode": "BLE_LIVE", "device_name": "",
        })


# ─────────────────────────── 세션 요청 ───────────────────────────

def test_session_id_must_match_generated_format():
    kst = timezone(timedelta(hours=9))
    ok = m.SessionStartRequest(
        request_id="r1", session_id="spacebio_20260724_100000_ab12",
        experiment_name="smoke", started_at=datetime(2026, 7, 24, tzinfo=kst),
    )
    assert ok.session_id.startswith("spacebio_")
    for bad in ("smoke", "spacebio_2026_100000_ab12", "spacebio_20260724_100000_"):
        with pytest.raises(ValidationError):
            m.SessionStartRequest(
                request_id="r1", session_id=bad, experiment_name="smoke",
                started_at=datetime(2026, 7, 24, tzinfo=kst),
            )


def test_session_update_carries_run_id():
    req = m.SessionUpdateRequest(
        request_id="r1", session_id="spacebio_20260724_100000_ab12",
        clinostat_run_id="run_0007",
    )
    assert req.clinostat_run_id == "run_0007"


def test_session_finish_allows_null_run_id():
    kst = timezone(timedelta(hours=9))
    req = m.SessionFinishRequest(
        request_id="r1", session_id="spacebio_20260724_100000_ab12",
        finished_at=datetime(2026, 7, 24, 11, 0, tzinfo=kst), clinostat_run_id=None,
    )
    assert req.clinostat_run_id is None


def test_experiment_name_rejects_blank():
    kst = timezone(timedelta(hours=9))
    with pytest.raises(ValidationError):
        m.SessionStartRequest(
            request_id="r1", session_id="spacebio_20260724_100000_ab12",
            experiment_name="   ", started_at=datetime(2026, 7, 24, tzinfo=kst),
        )


# ─────────────────────────── 응답 envelope ───────────────────────────

def test_success_envelope_has_required_fields():
    env = m.success_envelope(request_id="r1", data={"status": "ok", "uptime_s": 12.3})
    assert env["schema_version"] == 1
    assert env["request_id"] == "r1"
    assert env["data"] == {"status": "ok", "uptime_s": 12.3}
    datetime.fromisoformat(env["server_time"])          # RFC 3339 파싱 가능
    assert datetime.fromisoformat(env["server_time"]).tzinfo is not None
    assert "error" not in env


def test_error_envelope_has_structured_error():
    env = m.error_envelope(
        request_id="r1", code="pump_estop_latched", message="operator reset required",
    )
    assert env["schema_version"] == 1
    assert env["error"] == {
        "code": "pump_estop_latched", "message": "operator reset required",
    }
    assert datetime.fromisoformat(env["server_time"]).tzinfo is not None
    assert "data" not in env


def test_error_envelope_never_leaks_traceback():
    env = m.error_envelope(
        request_id="r1", code="internal_error",
        message='Traceback (most recent call last):\n  File "/x.py", line 1',
    )
    assert "Traceback" not in env["error"]["message"]
    assert "/x.py" not in env["error"]["message"]


# ─────────────────────────── 상태 스키마 ───────────────────────────

def test_sensor_sample_carries_the_stored_fields():
    sample = m.SensorSample(
        source_timestamp_ms=1784854800000, session_elapsed_ms=1200, loop_count=0,
        raw_adc=2041, resistance_ohm=82430.2, delta_r_over_r0=0.0018,
        temperature_c=25.1, battery_pct=91,
    )
    assert sample.model_dump()["raw_adc"] == 2041


def _sample(**overrides):
    payload = dict(
        source_timestamp_ms=0, session_elapsed_ms=0, loop_count=0, raw_adc=0,
        resistance_ohm=1.0, delta_r_over_r0=0.0, temperature_c=25.0, battery_pct=50,
    )
    payload.update(overrides)
    return m.SensorSample(**payload)


@pytest.mark.parametrize("adc", [-32768, -13263, 0, 2041, 12985, 32767])
def test_sensor_sample_accepts_signed_int16_adc(adc):
    """센서 노드는 ADS1115(16비트 차동)라 raw_adc가 음수와 4095 초과를 포함한다.

    실측값 -13263 / 12985가 여기 들어 있다 — 0-4095로 제한하면 실측 CSV
    재생이 전부 거부된다(2026-07-24 실제로 발생).
    """
    assert _sample(raw_adc=adc).raw_adc == adc


@pytest.mark.parametrize("adc", [-32769, 32768])
def test_sensor_sample_rejects_adc_outside_int16(adc):
    with pytest.raises(ValidationError):
        _sample(raw_adc=adc)


def test_synthetic_adc_full_scale_is_config_default_not_schema_bound():
    """스펙 6.3의 0-4095는 합성 생성기 clamp용 기본값이지 저장 스키마 제약이 아니다."""
    assert m.SYNTHETIC_ADC_FULL_SCALE == 4095
    assert m.RAW_ADC_MAX > m.SYNTHETIC_ADC_FULL_SCALE
    _sample(raw_adc=m.SYNTHETIC_ADC_FULL_SCALE + 1)      # 스키마는 거부하지 않는다


def test_real_thinkpad_dataset_round_trips_through_sensor_sample():
    """실측 CSV 전체가 SensorSample을 통과하는지 확인한다.

    합성 fixture만으로는 못 잡는다 — 실제 데이터로 확인해야 하는 회귀 테스트.
    """
    import csv as _csv
    from pathlib import Path

    dataset = (Path(__file__).resolve().parents[1]
               / "datasets" / "thinkpad_20260714_172138_ble_test.csv")
    if not dataset.exists():
        pytest.skip("실측 dataset이 반입되지 않음")

    with dataset.open(newline="", encoding="utf-8") as handle:
        rows = [r for r in _csv.DictReader(handle) if any(v.strip() for v in r.values())]

    assert rows, "실측 dataset이 비어 있다"
    for index, row in enumerate(rows):
        m.SensorSample(
            source_timestamp_ms=int(row["timestamp_ms"]), session_elapsed_ms=index,
            loop_count=0, raw_adc=int(row["raw_adc"]),
            resistance_ohm=float(row["resistance_ohm"]),
            delta_r_over_r0=float(row["delta_r_over_r0"]),
            temperature_c=float(row["temperature_c"]),
            battery_pct=int(row["battery_pct"]),
        )


def test_pump_status_defaults_to_simulated_and_idle():
    status = m.PumpStatus()
    assert status.mode == "SIMULATED"
    assert status.state is m.PumpState.IDLE
    assert status.estop_latched is False
    assert status.delivered_volume_ul == 0.0
    assert status.session_cumulative_volume_ul == 0.0
    assert status.target_volume_ul is None


def test_gateway_status_composes_all_components():
    status = m.GatewayStatus(
        gateway=m.GatewayComponent(state=m.GatewayState.ONLINE),
        sensor=m.SensorStatus(),
        pump=m.PumpStatus(),
        session=m.SessionStatus(),
    )
    dumped = status.model_dump()
    assert set(dumped) == {"gateway", "sensor", "pump", "session"}
    assert dumped["session"]["session_id"] is None


def test_websocket_message_shape():
    msg = m.StatusMessage(
        sequence=7,
        data=m.GatewayStatus(
            gateway=m.GatewayComponent(state=m.GatewayState.ONLINE),
            sensor=m.SensorStatus(), pump=m.PumpStatus(), session=m.SessionStatus(),
        ),
    )
    dumped = msg.model_dump()
    assert dumped["type"] == "status"
    assert dumped["sequence"] == 7
    assert set(dumped["data"]) == {"gateway", "sensor", "pump", "session"}


# ─────────────────────────── 실기 확장 (2026-07-25) ───────────────────────────

def test_serial_live_sample_allows_null_raw_adc():
    """실기 센서는 raw ADC를 내지 않는다 — None이 허용돼야 실측 스트림이 기록된다."""
    s = m.SensorSample(
        source_timestamp_ms=15821367, session_elapsed_ms=0, resistance_ohm=1_000_000.0,
        delta_r_over_r0=0.0, temperature_c=25.0, battery_pct=0,
    )
    assert s.raw_adc is None


def test_pump_step_request_bounds_match_firmware():
    assert m.PumpStepRequest(steps=200, spm=600).steps == 200
    for bad in ({"steps": 0, "spm": 600}, {"steps": 200, "spm": 0},
                {"steps": 200, "spm": 2401}):
        with pytest.raises(ValidationError):
            m.PumpStepRequest(**bad)


def test_pump_step_request_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        m.PumpStepRequest(steps=200, spm=600, target_volume_ul=100.0)


def test_pump_status_carries_wireless_step_telemetry():
    status = m.PumpStatus(mode=m.PUMP_MODE_WIRELESS, position_steps=782, spm=2400)
    assert status.mode == "WIRELESS"
    assert status.position_steps == 782
    assert status.spm == 2400


def test_datasets_response_hides_paths():
    resp = m.DatasetsResponse(datasets=(
        m.DatasetInfo(dataset_id="d1", sample_count=522, provenance="ThinkPad"),
    ))
    dumped = resp.model_dump()
    assert dumped["datasets"][0]["dataset_id"] == "d1"
    assert "filename" not in dumped["datasets"][0]
    assert "path" not in dumped["datasets"][0]
