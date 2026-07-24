"""Gateway FastAPI REST 계약 테스트 (설계 스펙 6장).

사이클 1: 라우트 계약 — envelope 형식, 센서/펌프/세션 REST 흐름, 상태 코드
매핑(409/422/503), 요청 본문 상한, config.yaml 고정값, 0.0.0.0 바인딩 금지.
사이클 3(라이프사이클)의 "센서·펌프 자동 재개 금지" 테스트도 여기 둔다 —
REST 응답만으로 검증할 수 있어 별도 사이클을 새로 열 필요가 없었다.
"""

from __future__ import annotations

import json
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.runtime import GatewayConfig, load_config
from tests.conftest import REPO_ROOT, make_gw_config

CSV_DATASET_ID = "thinkpad_20260714_172138_ble_test"


def _synthetic_configure_payload(request_id: str = "cfg-1") -> dict:
    return {
        "request_id": request_id, "mode": "SYNTHETIC",
        "baseline_resistance_ohm": 80_000.0, "amplitude_ohm": 1000.0,
        "period_s": 10.0, "noise_std_ohm": 10.0, "seed": 42,
    }


def _session_start_payload(session_id="spacebio_20260724_120000_ab12", request_id="s-1") -> dict:
    return {
        "request_id": request_id, "session_id": session_id,
        "experiment_name": "smoke-test", "started_at": "2026-07-24T12:00:00+09:00",
    }


# ─────────────────────────── envelope / 상태 조회 ───────────────────────────

def test_health_returns_ok_envelope(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == {"status": "ok", "uptime_s": pytest.approx(body["data"]["uptime_s"])}
    assert body["data"]["status"] == "ok"
    assert body["data"]["uptime_s"] >= 0
    assert "schema_version" in body and "request_id" in body and "server_time" in body


def test_request_id_header_is_echoed_and_generated_when_absent(client: TestClient):
    resp = client.get("/health", headers={"X-Request-ID": "my-req-1"})
    assert resp.json()["request_id"] == "my-req-1"

    resp2 = client.get("/health")
    assert resp2.json()["request_id"]  # server generated a UUID


def test_status_routes_expose_full_and_per_component_views(client: TestClient):
    full = client.get("/api/status").json()["data"]
    assert set(full.keys()) == {"gateway", "sensor", "pump", "session"}

    sensor = client.get("/api/sensor/status").json()["data"]
    assert sensor == full["sensor"]

    pump = client.get("/api/pump/status").json()["data"]
    assert pump == full["pump"]

    session = client.get("/api/session/status").json()["data"]
    assert session == full["session"]


# ─────────────────────────── 센서 흐름 ───────────────────────────

def test_sensor_configure_start_stop_flow(client: TestClient):
    configured = client.post("/api/sensor/configure", json=_synthetic_configure_payload()).json()
    assert configured["data"]["state"] == "idle"
    assert configured["data"]["mode"] == "SYNTHETIC"

    started = client.post(
        "/api/sensor/start", json={"request_id": "start-1"},
    ).json()
    assert started["data"]["state"] == "running"

    stopped = client.post("/api/sensor/stop", json={"request_id": "stop-1"}).json()
    assert stopped["data"]["state"] == "stopped"

    # 정지는 멱등이어야 한다.
    stopped_again = client.post("/api/sensor/stop", json={"request_id": "stop-2"})
    assert stopped_again.status_code == 200
    assert stopped_again.json()["data"]["state"] == "stopped"


def test_sensor_configure_with_unknown_dataset_id_is_422(client: TestClient):
    payload = {
        "request_id": "cfg-bad", "mode": "CSV_REPLAY", "dataset_id": "does_not_exist",
    }
    resp = client.post("/api/sensor/configure", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_sensor_start_while_running_is_409(client: TestClient):
    client.post("/api/sensor/configure", json=_synthetic_configure_payload())
    client.post("/api/sensor/start", json={"request_id": "start-a"})
    resp = client.post("/api/sensor/start", json={"request_id": "start-b"})
    assert resp.status_code == 409


def test_sensor_configure_csv_dataset_from_real_manifest(client: TestClient):
    payload = {"request_id": "cfg-csv", "mode": "CSV_REPLAY", "dataset_id": CSV_DATASET_ID}
    resp = client.post("/api/sensor/configure", json=payload)
    assert resp.status_code == 200
    assert resp.json()["data"]["mode"] == "CSV_REPLAY"


def test_sensor_configure_rejects_path_traversal_dataset_id(client: TestClient):
    payload = {"request_id": "cfg-evil", "mode": "CSV_REPLAY", "dataset_id": "../etc/passwd"}
    resp = client.post("/api/sensor/configure", json=payload)
    assert resp.status_code == 422


def test_sensor_start_without_configure_is_409(client: TestClient):
    resp = client.post("/api/sensor/start", json={"request_id": "start-no-cfg"})
    assert resp.status_code == 409


# ─────────────────────────── 펌프 흐름 / 상태 코드 매핑 ───────────────────────────

def test_pump_dispense_then_stop(client: TestClient):
    resp = client.post(
        "/api/pump/dispense",
        json={"request_id": "d-1", "rate_ul_s": 200.0, "target_volume_ul": 1.0},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["state"] == "running"

    stopped = client.post("/api/pump/stop", json={"request_id": "d-stop"})
    assert stopped.status_code == 200
    assert stopped.json()["data"]["state"] == "stopped"

    stopped_again = client.post("/api/pump/stop", json={"request_id": "d-stop-2"})
    assert stopped_again.status_code == 200  # 정지는 멱등


def test_pump_dispense_out_of_range_is_422(client: TestClient):
    resp = client.post(
        "/api/pump/dispense",
        json={"request_id": "d-bad", "rate_ul_s": 999.0, "target_volume_ul": 1.0},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_pump_dispense_while_estop_latched_is_409(client: TestClient):
    estop = client.post("/api/pump/emergency-stop", json={"request_id": "e-1"})
    assert estop.status_code == 200
    assert estop.json()["data"]["estop_latched"] is True

    resp = client.post(
        "/api/pump/dispense",
        json={"request_id": "d-2", "rate_ul_s": 50.0, "target_volume_ul": 10.0},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "pump_estop_latched"


def test_pump_emergency_stop_is_idempotent(client: TestClient):
    first = client.post("/api/pump/emergency-stop", json={"request_id": "e-a"})
    second = client.post("/api/pump/emergency-stop", json={"request_id": "e-b"})
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["data"]["state"] == "emergency_stopped"


def test_pump_reset_emergency_stop_requires_exact_acknowledgement(client: TestClient):
    client.post("/api/pump/emergency-stop", json={"request_id": "e-1"})

    bad = client.post(
        "/api/pump/reset-emergency-stop",
        json={"request_id": "r-bad", "acknowledgement": "nope"},
    )
    assert bad.status_code == 422

    good = client.post(
        "/api/pump/reset-emergency-stop",
        json={"request_id": "r-ok", "acknowledgement": "RESET_SIMULATED_PUMP_ESTOP"},
    )
    assert good.status_code == 200
    data = good.json()["data"]
    assert data["previous_state"] == "emergency_stopped"
    assert data["state"] == "idle"
    assert data["estop_latched"] is False
    assert data["accepted"] is True


def test_pump_dispense_same_request_id_replays_original_response(client: TestClient):
    first = client.post(
        "/api/pump/dispense",
        json={"request_id": "dup-1", "rate_ul_s": 50.0, "target_volume_ul": 10.0},
    )
    second = client.post(
        "/api/pump/dispense",
        json={"request_id": "dup-1", "rate_ul_s": 50.0, "target_volume_ul": 10.0},
    )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"] == second.json()["data"]


def test_pump_reset_emergency_stop_is_idempotent_when_not_latched(client: TestClient):
    resp = client.post(
        "/api/pump/reset-emergency-stop",
        json={"request_id": "r-noop", "acknowledgement": "RESET_SIMULATED_PUMP_ESTOP"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["state"] == "idle"
    assert data["estop_latched"] is False
    assert data["accepted"] is True


def test_pump_fault_from_broken_estop_persistence_is_503(tmp_path):
    """state_root 자리에 파일을 만들어 두면 estop persistence.save()가 실패한다."""
    blocked_state_root = tmp_path / "state_is_a_file"
    blocked_state_root.write_text("not a directory", encoding="utf-8")
    config = make_gw_config(tmp_path, state_root=blocked_state_root)
    app = create_app(config)
    with TestClient(app) as broken_client:
        resp = broken_client.post("/api/pump/emergency-stop", json={"request_id": "e-fault"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "device_fault"


# ─────────────────────────── 세션 흐름 ───────────────────────────

def test_session_start_update_finish_flow(client: TestClient):
    started = client.post("/api/session/start", json=_session_start_payload())
    assert started.status_code == 200
    assert started.json()["data"]["state"] == "recording"

    updated = client.post(
        "/api/session/update",
        json={"request_id": "u-1", "session_id": "spacebio_20260724_120000_ab12",
              "clinostat_run_id": "run-42"},
    )
    assert updated.status_code == 200

    finished = client.post(
        "/api/session/finish",
        json={"request_id": "f-1", "session_id": "spacebio_20260724_120000_ab12",
              "finished_at": "2026-07-24T13:00:00+09:00"},
    )
    assert finished.status_code == 200
    assert finished.json()["data"]["state"] == "completed"

    # 종료는 멱등이어야 한다 — 같은 request_id 재전송은 캐시된 결과를 낸다.
    finished_again = client.post(
        "/api/session/finish",
        json={"request_id": "f-1", "session_id": "spacebio_20260724_120000_ab12",
              "finished_at": "2026-07-24T13:00:00+09:00"},
    )
    assert finished_again.status_code == 200
    assert finished_again.json()["data"] == finished.json()["data"]


def test_session_update_same_run_id_is_idempotent_different_run_id_is_409(client: TestClient):
    session_id = "spacebio_20260724_120000_ab12"
    client.post("/api/session/start", json=_session_start_payload(session_id=session_id))

    first = client.post(
        "/api/session/update",
        json={"request_id": "u-1", "session_id": session_id, "clinostat_run_id": "run-42"},
    )
    assert first.status_code == 200

    same_again = client.post(
        "/api/session/update",
        json={"request_id": "u-2", "session_id": session_id, "clinostat_run_id": "run-42"},
    )
    assert same_again.status_code == 200

    different = client.post(
        "/api/session/update",
        json={"request_id": "u-3", "session_id": session_id, "clinostat_run_id": "run-99"},
    )
    assert different.status_code == 409


def test_session_status_for_unknown_session_is_explicit_idle(client: TestClient):
    resp = client.get(
        "/api/session/status",
        params={"session_id": "spacebio_20990101_000000_zzzz", "request_id": "never-seen"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {"state": "idle", "session_id": None, "experiment_name": None}


def test_session_finish_writes_expected_file_formats(tmp_path):
    config = make_gw_config(tmp_path)
    app = create_app(config)
    session_id = "spacebio_20260724_140000_ef56"
    with TestClient(app) as file_client:
        file_client.post("/api/sensor/configure", json=_synthetic_configure_payload("cfg-file"))
        file_client.post("/api/sensor/start", json={"request_id": "start-file"})
        file_client.post(
            "/api/session/start",
            json=_session_start_payload(session_id=session_id, request_id="start-sess-file"),
        )
        time.sleep(0.25)  # 10Hz 센서 tick이 최소 한 번은 세션 저장소에 기록되게 한다
        file_client.post(
            "/api/pump/dispense",
            json={"request_id": "pd-file", "rate_ul_s": 200.0, "target_volume_ul": 1.0},
        )
        file_client.post("/api/pump/stop", json={"request_id": "pstop-file"})
        file_client.post(
            "/api/session/finish",
            json={"request_id": "finish-file", "session_id": session_id,
                  "finished_at": "2026-07-24T14:05:00+09:00"},
        )

    session_dir = config.data_root / "sessions" / session_id
    csv_text = (session_dir / "sensor_samples.csv").read_text(encoding="utf-8")
    assert csv_text.startswith(
        "schema_version,session_id,source_mode,source_timestamp_ms"
    )

    lines = (session_dir / "pump_events.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines
    events = [json.loads(line) for line in lines]
    for event in events:
        assert {"at", "ts_ms", "previous_state", "cause"} <= event.keys()


def test_session_start_twice_is_409(client: TestClient):
    client.post("/api/session/start", json=_session_start_payload(request_id="s-1"))
    resp = client.post(
        "/api/session/start",
        json=_session_start_payload(session_id="spacebio_20260724_130000_cd34", request_id="s-2"),
    )
    assert resp.status_code == 409


def test_session_status_reconciliation_by_request_id(client: TestClient):
    payload = _session_start_payload(request_id="reconcile-1")
    started = client.post("/api/session/start", json=payload).json()

    resp = client.get(
        "/api/session/status",
        params={"session_id": payload["session_id"], "request_id": "reconcile-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == started["data"]


def test_session_update_without_active_session_is_409(client: TestClient):
    resp = client.post(
        "/api/session/update",
        json={"request_id": "u-orphan", "session_id": "spacebio_20260724_120000_ab12",
              "clinostat_run_id": "run-1"},
    )
    assert resp.status_code == 409


def test_error_bodies_never_leak_tracebacks(client: TestClient):
    responses = [
        client.post("/api/sensor/start", json={"request_id": "no-cfg"}),
        client.post(
            "/api/pump/dispense",
            json={"request_id": "bad-rate", "rate_ul_s": 999.0, "target_volume_ul": 1.0},
        ),
        client.post(
            "/api/sensor/configure",
            json={"request_id": "bad-ds", "mode": "CSV_REPLAY", "dataset_id": "nope"},
        ),
    ]
    for resp in responses:
        assert resp.status_code in (409, 422)
        assert "Traceback" not in resp.text
        assert "File \"" not in resp.text


# ─────────────────────────── 요청 본문 상한 ───────────────────────────

def test_request_body_over_64kib_is_rejected(client: TestClient):
    huge_name = "x" * (65 * 1024)
    resp = client.post(
        "/api/session/start",
        json={
            "request_id": "huge", "session_id": "spacebio_20260724_120000_ab12",
            "experiment_name": huge_name, "started_at": "2026-07-24T12:00:00+09:00",
        },
    )
    assert resp.status_code == 413


# ─────────────────────────── 라이프사이클: 자동 재개 금지 ───────────────────────────

def test_startup_recovers_metadata_but_never_auto_resumes(tmp_path):
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    session_id = "spacebio_20260724_090000_old1"
    session_dir = data_root / "sessions" / session_id
    session_dir.mkdir(parents=True)

    manifest = {
        "schema_version": 1, "session_id": session_id, "experiment_name": "prior-run",
        "started_at": "2026-07-24T09:00:00+09:00", "finished_at": None,
        "status": "recording", "clinostat_run_id": None, "sensor_mode": "SYNTHETIC",
        "pump_mode": "SIMULATED", "errors": [],
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    pump_event = {
        "schema_version": 1, "session_id": session_id, "at": "2026-07-24T09:05:00+09:00",
        "ts_ms": 1784854800000, "previous_state": "running", "new_state": "emergency_stopped",
        "cause": "emergency_stop", "request_id": "old-req", "delivered_volume_ul": 12.5,
    }
    (session_dir / "pump_events.jsonl").write_text(json.dumps(pump_event) + "\n", encoding="utf-8")

    state_root.mkdir(parents=True)
    (state_root / "estop_latch.json").write_text(
        json.dumps({"estop_latched": True}), encoding="utf-8",
    )

    config = make_gw_config(tmp_path, data_root=data_root, state_root=state_root)
    app = create_app(config)
    with TestClient(app) as recovered_client:
        status = recovered_client.get("/api/status").json()["data"]

        # 센서/펌프 실행은 절대 자동 재개되지 않는다.
        assert status["sensor"]["state"] == "idle"
        assert status["sensor"]["sample"] is None

        # 비상정지 래치는 SimulatedPump 자신의 영속화 파일에서 독립적으로 복원된다.
        assert status["pump"]["estop_latched"] is True
        assert status["pump"]["state"] == "emergency_stopped"
        assert status["pump"]["delivered_volume_ul"] == 0.0

        # 세션 메타데이터는 복원되지만, 실제로 다시 열려 기록 중인 것은 아니다 —
        # 새 세션을 시작할 수 있어야 증명된다(예전 디렉터리를 다시 쓰지 않는다).
        assert status["session"]["session_id"] == session_id

        new_session = recovered_client.post(
            "/api/session/start",
            json={
                "request_id": "new-1", "session_id": "spacebio_20260724_100000_new1",
                "experiment_name": "post-restart", "started_at": "2026-07-24T10:00:00+09:00",
            },
        )
        assert new_session.status_code == 200


# ─────────────────────────── 바인딩 안전 제약 ───────────────────────────

def test_config_yaml_never_binds_0000_and_gateway_section_is_fixed():
    text = (REPO_ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "0.0.0.0" not in text

    raw = yaml.safe_load(text)
    assert raw["gateway"] == {
        "host": "127.0.0.1", "port": 8010, "sensor_publish_hz": 10,
        "browser_publish_hz": 5, "min_free_bytes": 524288000,
    }
    assert raw["sensor"] == {
        "datasets_dir": "./datasets", "adc_full_scale": 4095,
        "reference_resistor_ohm": 82500.0, "default_temperature_c": 25.0,
        "default_battery_pct": 100,
    }
    assert raw["coordinator"] == {"state_root": "/home/aiworker-1/clinostat/spacebio-state"}
    # DEC-012로 동결된 구 섹션들은 지워지지 않았다.
    assert "mqtt" in raw and "ble" in raw and "loop" in raw


def test_load_config_matches_spec_defaults():
    config: GatewayConfig = load_config(REPO_ROOT / "config.yaml")
    assert config.host == "127.0.0.1"
    assert config.port == 8010
    assert config.sensor_publish_hz == 10
    assert config.browser_publish_hz == 5
    assert config.min_free_bytes == 524_288_000
    assert config.pump_min_volume_ul == 1.0
    assert config.pump_max_volume_ul == 1000.0
    assert config.pump_min_rate_ul_s == 1.0
    assert config.pump_max_rate_ul_s == 200.0


def test_app_module_never_binds_0000():
    """`app.py`는 host/port를 직접 고르지 않고 항상 GatewayConfig에서 읽는다.

    (`runtime.py`의 `load_config`는 0.0.0.0을 *거부*하기 위해 그 문자열을
    비교 대상으로 참조하므로 여기서 검사하지 않는다 — 바인딩이 아니라 방어다.)
    """
    text = (REPO_ROOT / "gateway" / "app.py").read_text(encoding="utf-8")
    assert "0.0.0.0" not in text


def test_load_config_rejects_0000_host(tmp_path):
    bad_config_path = tmp_path / "config.yaml"
    bad_config_path.write_text(
        yaml.safe_dump({"gateway": {"host": "0.0.0.0", "port": 8010}}), encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(bad_config_path)
