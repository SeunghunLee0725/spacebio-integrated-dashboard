"""세션 저장·재시작 복구 테스트 — 설계 스펙 5 / 6.8.

사이클 1: 저장 (SessionStore) — manifest 원자성, CSV/JSONL 포맷, flush/fsync 정책,
거부 조건(409/여유공간), disk-full 전이.
사이클 2: 복구 (recover_session) — estop 래치, 누적량, 마지막 상태, 불완전 줄 처리,
센서/펌프 자동 재개 금지.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.api_models import (
    SensorSample,
    SessionFinishRequest,
    SessionStartRequest,
    SessionUpdateRequest,
)
from gateway.session_store import (
    PumpEvent,
    SessionConflictError,
    SessionIoError,
    SessionNotActiveError,
    SessionStore,
    InsufficientSpaceError,
)
from gateway.recovery import recover_session

KST = timezone(timedelta(hours=9))


def _start_request(session_id="spacebio_20260724_100000_ab12", experiment_name="simulation-smoke"):
    return SessionStartRequest(
        request_id="r1", session_id=session_id, experiment_name=experiment_name,
        started_at=datetime(2026, 7, 24, 10, 0, 0, tzinfo=KST),
    )


def _finish_request(session_id="spacebio_20260724_100000_ab12", clinostat_run_id=None):
    return SessionFinishRequest(
        request_id="r2", session_id=session_id,
        finished_at=datetime(2026, 7, 24, 11, 0, 0, tzinfo=KST),
        clinostat_run_id=clinostat_run_id,
    )


def _sample(elapsed=1000, loop_count=0):
    return SensorSample(
        source_timestamp_ms=1784854800000, session_elapsed_ms=elapsed, loop_count=loop_count,
        raw_adc=2041, resistance_ohm=82430.2, delta_r_over_r0=0.0018,
        temperature_c=25.1, battery_pct=91,
    )


def _pump_event(new_state="running", delivered=10.0, prev="idle", request_id="pr1", cause="dispense"):
    return PumpEvent(
        ts_ms=1784854800000, previous_state=prev, new_state=new_state,
        cause=cause, request_id=request_id, delivered_volume_ul=delivered,
    )


def _ample_free_space(_path) -> int:
    return 10 * 1024 * 1024 * 1024  # 10 GiB


def _make_store(tmp_path, **kwargs):
    kwargs.setdefault("free_space_bytes", _ample_free_space)
    return SessionStore(tmp_path, **kwargs)


# ─────────────────────────── 사이클 1: 저장 ───────────────────────────

def test_start_creates_session_layout_and_manifest(tmp_path):
    store = _make_store(tmp_path)
    snapshot = store.start(_start_request())

    session_dir = tmp_path / "sessions" / "spacebio_20260724_100000_ab12"
    assert session_dir.is_dir()
    assert (session_dir / "manifest.json").exists()
    assert (session_dir / "sensor_samples.csv").exists()
    assert (session_dir / "pump_events.jsonl").exists()

    manifest = json.loads((session_dir / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["session_id"] == "spacebio_20260724_100000_ab12"
    assert manifest["experiment_name"] == "simulation-smoke"
    assert manifest["status"] == "recording"
    assert manifest["finished_at"] is None
    assert manifest["clinostat_run_id"] is None
    assert manifest["sensor_mode"] == "CSV_REPLAY"
    assert manifest["pump_mode"] == "SIMULATED"
    assert manifest["errors"] == []
    assert snapshot.status == "recording"


def test_start_rejects_existing_session_directory_with_409(tmp_path):
    store = _make_store(tmp_path)
    store.start(_start_request())
    store.finish(_finish_request())

    other_store = _make_store(tmp_path)
    with pytest.raises(SessionConflictError):
        other_store.start(_start_request())


def test_start_rejects_when_free_space_below_500_mib(tmp_path):
    below_threshold = lambda _path: 400 * 1024 * 1024  # 400 MiB < 500 MiB
    store = SessionStore(tmp_path, free_space_bytes=below_threshold)
    with pytest.raises(InsufficientSpaceError):
        store.start(_start_request())
    assert not (tmp_path / "sessions" / "spacebio_20260724_100000_ab12").exists()


def test_sensor_csv_header_is_written_exactly_once(tmp_path):
    store = _make_store(tmp_path)
    store.start(_start_request())
    store.append_sensor(_sample(elapsed=100))
    store.append_sensor(_sample(elapsed=200))
    store.finish(_finish_request())

    lines = (tmp_path / "sessions" / "spacebio_20260724_100000_ab12" / "sensor_samples.csv").read_text().splitlines()
    expected_header = (
        "schema_version,session_id,source_mode,source_timestamp_ms,session_elapsed_ms,"
        "loop_count,raw_adc,resistance_ohm,delta_r_over_r0,temperature_c,battery_pct"
    )
    assert lines[0] == expected_header
    assert lines.count(expected_header) == 1
    assert len(lines) == 3  # header + 2 rows
    assert lines[1].split(",")[4] == "100"  # session_elapsed_ms
    assert lines[1].split(",")[2] == "CSV_REPLAY"  # source_mode


def test_pump_jsonl_has_one_complete_json_object_per_line(tmp_path):
    store = _make_store(tmp_path)
    store.start(_start_request())
    store.append_pump_event(_pump_event(new_state="running", delivered=5.0))
    store.append_pump_event(_pump_event(prev="running", new_state="completed", delivered=15.0))
    store.finish(_finish_request())

    lines = (tmp_path / "sessions" / "spacebio_20260724_100000_ab12" / "pump_events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["previous_state"] == "idle"
    assert first["new_state"] == "running"
    assert first["request_id"] == "pr1"
    assert first["delivered_volume_ul"] == 5.0
    second = json.loads(lines[1])
    assert second["new_state"] == "completed"
    assert second["delivered_volume_ul"] == 15.0


def test_append_sensor_requires_active_session(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(SessionNotActiveError):
        store.append_sensor(_sample())


def test_update_run_id_is_idempotent_for_same_id(tmp_path):
    store = _make_store(tmp_path)
    store.start(_start_request())
    req = SessionUpdateRequest(
        request_id="u1", session_id="spacebio_20260724_100000_ab12", clinostat_run_id="run_0007",
    )
    first = store.update_run_id(req)
    second = store.update_run_id(req)
    assert first.clinostat_run_id == "run_0007"
    assert second.clinostat_run_id == "run_0007"


def test_update_run_id_rejects_replacement_with_409(tmp_path):
    store = _make_store(tmp_path)
    store.start(_start_request())
    store.update_run_id(SessionUpdateRequest(
        request_id="u1", session_id="spacebio_20260724_100000_ab12", clinostat_run_id="run_0007",
    ))
    with pytest.raises(SessionConflictError):
        store.update_run_id(SessionUpdateRequest(
            request_id="u2", session_id="spacebio_20260724_100000_ab12", clinostat_run_id="run_0008",
        ))


def test_finish_marks_completed_and_fsyncs(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    store.start(_start_request())
    store.append_sensor(_sample())

    fsync_calls = []
    monkeypatch.setattr(os, "fsync", lambda fd: fsync_calls.append(fd))

    snapshot = store.finish(_finish_request())
    assert snapshot.status == "completed"
    assert snapshot.finished_at is not None
    assert len(fsync_calls) >= 2  # sensor csv + pump jsonl

    manifest = json.loads((tmp_path / "sessions" / "spacebio_20260724_100000_ab12" / "manifest.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["finished_at"] is not None


def test_flush_triggers_at_100_records_not_before(tmp_path):
    store = _make_store(tmp_path)
    store.start(_start_request())

    flush_calls = {"n": 0}
    real_flush = store._sensor_fh.flush
    def counting_flush():
        flush_calls["n"] += 1
        real_flush()
    store._sensor_fh.flush = counting_flush

    for i in range(99):
        store.append_sensor(_sample(elapsed=i))
    assert store._sensor_count == 99
    assert flush_calls["n"] == 0  # header write already flushed once at open; reset baseline
    header_flush_baseline = flush_calls["n"]

    store.append_sensor(_sample(elapsed=99))
    assert store._sensor_count == 0  # counter reset after threshold-triggered flush
    assert flush_calls["n"] == header_flush_baseline + 1


def test_flush_triggers_after_one_second_elapsed(tmp_path):
    clock = {"t": 0.0}
    store = _make_store(tmp_path, clock=lambda: clock["t"])
    store.start(_start_request())

    flush_calls = {"n": 0}
    real_flush = store._sensor_fh.flush
    def counting_flush():
        flush_calls["n"] += 1
        real_flush()
    store._sensor_fh.flush = counting_flush
    baseline = flush_calls["n"]

    store.append_sensor(_sample(elapsed=1))
    assert flush_calls["n"] == baseline  # not yet 1s elapsed, well under 100 records

    clock["t"] += 1.5
    store.append_sensor(_sample(elapsed=2))
    assert flush_calls["n"] == baseline + 1
    assert store._sensor_count == 0


def test_pump_event_fsyncs_immediately_on_emergency_stop(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    store.start(_start_request())

    fsync_calls = []
    monkeypatch.setattr(os, "fsync", lambda fd: fsync_calls.append(fd))

    store.append_pump_event(_pump_event(new_state="running", delivered=1.0))
    assert len(fsync_calls) == 0  # ordinary event -> no immediate fsync

    store.append_pump_event(_pump_event(prev="running", new_state="emergency_stopped", delivered=1.0))
    assert len(fsync_calls) == 1


def test_io_error_transitions_session_to_partial_and_records_error(tmp_path):
    store = _make_store(tmp_path)
    store.start(_start_request())

    def broken_writerow(*_args, **_kwargs):
        raise OSError("No space left on device")
    store._sensor_fh.write = lambda *a, **k: (_ for _ in ()).throw(OSError("No space left on device"))

    with pytest.raises(SessionIoError):
        store.append_sensor(_sample())

    snapshot = store.snapshot()
    assert snapshot.status == "partial"
    assert any("No space left on device" in err for err in snapshot.errors)

    manifest = json.loads((tmp_path / "sessions" / "spacebio_20260724_100000_ab12" / "manifest.json").read_text())
    assert manifest["status"] == "partial"


# ─────────────────────────── 사이클 2: 복구 ───────────────────────────

def _write_manifest(session_dir: Path, **overrides) -> None:
    manifest = {
        "schema_version": 1, "session_id": session_dir.name,
        "experiment_name": "simulation-smoke",
        "started_at": "2026-07-24T10:00:00+09:00", "finished_at": None,
        "status": "recording", "clinostat_run_id": None,
        "sensor_mode": "CSV_REPLAY", "pump_mode": "SIMULATED", "errors": [],
    }
    manifest.update(overrides)
    (session_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _write_pump_events(session_dir: Path, lines: list[str], trailing_newline: bool = True) -> None:
    content = "\n".join(lines)
    if trailing_newline:
        content += "\n"
    (session_dir / "pump_events.jsonl").write_text(content, encoding="utf-8")


def test_recover_reads_manifest_and_last_pump_state(tmp_path):
    session_dir = tmp_path / "sessions" / "spacebio_20260724_100000_ab12"
    session_dir.mkdir(parents=True)
    _write_manifest(session_dir, status="completed", finished_at="2026-07-24T11:00:00+09:00")
    _write_pump_events(session_dir, [
        json.dumps({"new_state": "running", "delivered_volume_ul": 10.0}),
        json.dumps({"new_state": "completed", "delivered_volume_ul": 40.0}),
    ])

    result = recover_session(session_dir)
    assert result.session_id == "spacebio_20260724_100000_ab12"
    assert result.status == "completed"
    assert result.needs_reconciliation is False
    assert result.last_pump_state == "completed"
    assert result.session_cumulative_volume_ul == pytest.approx(50.0)
    assert result.estop_latched is False


def test_recover_detects_latched_estop(tmp_path):
    session_dir = tmp_path / "sessions" / "spacebio_20260724_100001_cd34"
    session_dir.mkdir(parents=True)
    _write_manifest(session_dir, session_id=session_dir.name, status="partial")
    _write_pump_events(session_dir, [
        json.dumps({"new_state": "running", "delivered_volume_ul": 5.0}),
        json.dumps({"new_state": "emergency_stopped", "delivered_volume_ul": 0.0}),
    ])

    result = recover_session(session_dir)
    assert result.estop_latched is True
    assert result.last_pump_state == "emergency_stopped"


@pytest.mark.parametrize("status", ["preparing", "recording"])
def test_recover_flags_preparing_and_recording_sessions_for_reconciliation(tmp_path, status):
    session_dir = tmp_path / "sessions" / f"spacebio_20260724_100002_{status[:4]}"
    session_dir.mkdir(parents=True)
    _write_manifest(session_dir, session_id=session_dir.name, status=status)

    result = recover_session(session_dir)
    assert result.needs_reconciliation is True
    assert result.status == status


def test_recover_flags_unknown_status_when_manifest_missing(tmp_path):
    session_dir = tmp_path / "sessions" / "spacebio_20260724_100003_efgh"
    session_dir.mkdir(parents=True)
    # manifest.json intentionally absent

    result = recover_session(session_dir)
    assert result.status == "unknown"
    assert result.needs_reconciliation is True


@pytest.mark.parametrize("status", ["completed", "partial", "failed"])
def test_recover_does_not_flag_terminal_statuses(tmp_path, status):
    session_dir = tmp_path / "sessions" / f"spacebio_20260724_100004_{status[:4]}"
    session_dir.mkdir(parents=True)
    _write_manifest(session_dir, session_id=session_dir.name, status=status,
                     finished_at="2026-07-24T11:00:00+09:00")

    result = recover_session(session_dir)
    assert result.needs_reconciliation is False


def test_recover_moves_incomplete_jsonl_tail_to_fragments_log_and_truncates(tmp_path):
    session_dir = tmp_path / "sessions" / "spacebio_20260724_100005_ijkl"
    session_dir.mkdir(parents=True)
    _write_manifest(session_dir, session_id=session_dir.name, status="recording")
    good_line = json.dumps({"new_state": "running", "delivered_volume_ul": 12.0})
    incomplete_line = '{"new_state": "completed", "delivered_vol'  # truncated mid-write, no newline
    (session_dir / "pump_events.jsonl").write_text(good_line + "\n" + incomplete_line, encoding="utf-8")

    result = recover_session(session_dir)

    assert result.last_pump_state == "running"  # incomplete line excluded from state
    assert result.session_cumulative_volume_ul == pytest.approx(12.0)
    assert len(result.fragments_recovered) == 1
    assert "completed" in result.fragments_recovered[0]

    fragments_log = (session_dir / "recovery_fragments.log").read_text(encoding="utf-8")
    assert incomplete_line in fragments_log

    remaining = (session_dir / "pump_events.jsonl").read_text(encoding="utf-8")
    assert remaining.strip().splitlines() == [good_line]
    assert incomplete_line not in remaining


def test_recover_moves_incomplete_csv_tail_to_fragments_log_and_truncates(tmp_path):
    session_dir = tmp_path / "sessions" / "spacebio_20260724_100006_mnop"
    session_dir.mkdir(parents=True)
    _write_manifest(session_dir, session_id=session_dir.name, status="recording")
    header = (
        "schema_version,session_id,source_mode,source_timestamp_ms,session_elapsed_ms,"
        "loop_count,raw_adc,resistance_ohm,delta_r_over_r0,temperature_c,battery_pct"
    )
    good_row = "1,spacebio_20260724_100006_mnop,CSV_REPLAY,1784854800000,1000,0,2041,82430.2,0.0018,25.1,91"
    incomplete_row = "1,spacebio_20260724_100006_mnop,CSV_REPLAY,1784854800500,1100,0,2041,824"  # cut short, no newline
    (session_dir / "sensor_samples.csv").write_text(
        header + "\n" + good_row + "\n" + incomplete_row, encoding="utf-8",
    )

    result = recover_session(session_dir)

    assert len(result.fragments_recovered) == 1
    assert incomplete_row in result.fragments_recovered[0]

    fragments_log = (session_dir / "recovery_fragments.log").read_text(encoding="utf-8")
    assert incomplete_row in fragments_log

    remaining_lines = (session_dir / "sensor_samples.csv").read_text(encoding="utf-8").splitlines()
    assert remaining_lines == [header, good_row]


def test_recovery_never_resumes_sensor_or_pump_execution(tmp_path):
    """복구는 메타데이터·래치만 복원한다 — 센서/펌프를 재개하는 부수효과가 없어야 한다."""
    session_dir = tmp_path / "sessions" / "spacebio_20260724_100007_qrst"
    session_dir.mkdir(parents=True)
    _write_manifest(session_dir, session_id=session_dir.name, status="recording")
    _write_pump_events(session_dir, [json.dumps({"new_state": "running", "delivered_volume_ul": 1.0})])

    manifest_before = (session_dir / "manifest.json").read_text(encoding="utf-8")
    pump_events_before = (session_dir / "pump_events.jsonl").read_text(encoding="utf-8")

    result = recover_session(session_dir)

    # 순수 조회: on-disk manifest/이벤트 로그를 재작성하거나 세션을 이어서 기록하지 않는다.
    assert (session_dir / "manifest.json").read_text(encoding="utf-8") == manifest_before
    assert (session_dir / "pump_events.jsonl").read_text(encoding="utf-8") == pump_events_before
    assert not hasattr(result, "resume")
    assert not hasattr(result, "sensor")
    assert not hasattr(result, "pump")
