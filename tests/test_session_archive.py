"""세션 아카이브 — 목록·다운로드·삭제의 계약과 안전장치.

삭제는 비가역이다. 여기서 지키는 것은 세 가지다:
  1. `session_id`는 정규식을 통과해야 하고, 조립한 경로가 sessions_root 밖으로
     나가면(`..`, 절대경로, 심볼릭 링크) 무조건 거부한다.
  2. **기록 중인 세션은 지울 수 없다** — 열려 있는 파일을 지우면 게이트웨이가
     쓰기를 계속하다 죽는다.
  3. 없는 세션은 404다 — 존재 여부를 오류 메시지로 흘리지 않는다.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from gateway.session_archive import (
    ArchiveConflictError,
    ArchiveNotFoundError,
    delete_session,
    list_sessions,
    open_archive,
)

VALID_ID = "spacebio_20260729_221916_resistancerun"
OTHER_ID = "spacebio_20260725_145647_d5y3"


def _make_session(root: Path, session_id: str, *, rows: int = 3, status: str = "completed") -> Path:
    directory = root / session_id
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "session_id": session_id,
            "experiment_name": "resistance-run",
            "started_at": "2026-07-29T13:19:16.440000+00:00",
            "finished_at": "2026-07-29T13:37:29.670000+00:00",
            "status": status,
            "sensor_mode": "BLE_LIVE",
            "pump_mode": "SIMULATED",
            "errors": [],
        }),
        encoding="utf-8",
    )
    header = "schema_version,session_id,resistance_ohm\n"
    body = "".join(f"1,{session_id},{100 + i}\n" for i in range(rows))
    (directory / "sensor_samples.csv").write_text(header + body, encoding="utf-8")
    (directory / "pump_events.jsonl").write_text("", encoding="utf-8")
    return directory


@pytest.fixture()
def sessions_root(tmp_path: Path) -> Path:
    root = tmp_path / "spacebio-data" / "sessions"
    root.mkdir(parents=True)
    return root


# ─────────────────────────── 목록 ───────────────────────────

def test_list_is_empty_when_nothing_recorded(sessions_root: Path):
    assert list_sessions(sessions_root) == []


def test_list_reports_manifest_fields_and_size(sessions_root: Path):
    directory = _make_session(sessions_root, VALID_ID, rows=3)

    (found,) = list_sessions(sessions_root)

    assert found.session_id == VALID_ID
    assert found.experiment_name == "resistance-run"
    assert found.status == "completed"
    assert found.sensor_mode == "BLE_LIVE"
    assert found.started_at == "2026-07-29T13:19:16.440000+00:00"
    assert found.finished_at == "2026-07-29T13:37:29.670000+00:00"
    # 헤더는 빼고 센다 — 사용자가 보는 것은 표본 수다.
    assert found.sample_rows == 3
    assert found.bytes == sum(p.stat().st_size for p in directory.iterdir())


def test_list_is_newest_first(sessions_root: Path):
    _make_session(sessions_root, OTHER_ID)
    _make_session(sessions_root, VALID_ID)

    assert [s.session_id for s in list_sessions(sessions_root)] == [VALID_ID, OTHER_ID]


def test_list_keeps_sessions_whose_manifest_is_unreadable(sessions_root: Path):
    """manifest가 깨져도 목록에는 나와야 지울 수 있다."""
    directory = sessions_root / VALID_ID
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text("{ not json", encoding="utf-8")

    (found,) = list_sessions(sessions_root)

    assert found.session_id == VALID_ID
    assert found.experiment_name is None
    assert found.status == "unknown"


def test_list_ignores_files_and_foreign_directory_names(sessions_root: Path):
    _make_session(sessions_root, VALID_ID)
    (sessions_root / "README.txt").write_text("x", encoding="utf-8")
    (sessions_root / "not-a-session").mkdir()

    assert [s.session_id for s in list_sessions(sessions_root)] == [VALID_ID]


def test_list_marks_the_active_session(sessions_root: Path):
    _make_session(sessions_root, VALID_ID, status="recording")
    _make_session(sessions_root, OTHER_ID)

    found = {s.session_id: s for s in list_sessions(sessions_root, active_session_id=VALID_ID)}

    assert found[VALID_ID].active is True
    assert found[OTHER_ID].active is False


def test_list_survives_a_missing_root(tmp_path: Path):
    """세션을 한 번도 안 만들었으면 sessions/ 자체가 없다."""
    assert list_sessions(tmp_path / "never-created") == []


# ─────────────────────────── 경로 안전장치 ───────────────────────────

@pytest.mark.parametrize("session_id", [
    "../../../etc",
    "/etc/passwd",
    "spacebio_20260729_221916_ok/../../escape",
    "spacebio_20260729_221916_ok/..",
    "not-a-session",
    "",
    ".",
    "spacebio_20260729_221916_한글",
])
def test_download_rejects_ids_that_could_escape_the_root(sessions_root: Path, session_id: str):
    with pytest.raises(ArchiveNotFoundError):
        open_archive(sessions_root, session_id)


@pytest.mark.parametrize("session_id", ["../../../etc", "/etc/passwd", "not-a-session"])
def test_delete_rejects_ids_that_could_escape_the_root(sessions_root: Path, session_id: str):
    with pytest.raises(ArchiveNotFoundError):
        delete_session(sessions_root, session_id, active_session_id=None)


def test_delete_refuses_to_follow_a_symlink_out_of_the_root(sessions_root: Path, tmp_path: Path):
    """링크를 지우면 링크만 지워지지만, 그 판단을 운에 맡기지 않는다."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me", encoding="utf-8")
    (sessions_root / VALID_ID).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArchiveNotFoundError):
        delete_session(sessions_root, VALID_ID, active_session_id=None)

    assert (outside / "precious.txt").exists()


# ─────────────────────────── 다운로드 ───────────────────────────

def test_download_packs_every_file_under_the_session_id(sessions_root: Path):
    _make_session(sessions_root, VALID_ID)

    stream, size = open_archive(sessions_root, VALID_ID)
    try:
        with tarfile.open(fileobj=stream, mode="r:gz") as tar:
            names = sorted(tar.getnames())
            payload = tar.extractfile(f"{VALID_ID}/sensor_samples.csv").read()
    finally:
        stream.close()

    assert names == [
        f"{VALID_ID}/manifest.json",
        f"{VALID_ID}/pump_events.jsonl",
        f"{VALID_ID}/sensor_samples.csv",
    ]
    assert payload.startswith(b"schema_version,session_id,resistance_ohm\n")
    assert size > 0


def test_download_reports_the_compressed_size(sessions_root: Path):
    _make_session(sessions_root, VALID_ID, rows=2000)

    stream, size = open_archive(sessions_root, VALID_ID)
    try:
        assert size == len(stream.read())
    finally:
        stream.close()


def test_download_of_a_missing_session_is_not_found(sessions_root: Path):
    with pytest.raises(ArchiveNotFoundError):
        open_archive(sessions_root, VALID_ID)


def test_download_of_the_active_session_is_allowed(sessions_root: Path):
    """기록 중에도 받아볼 수 있어야 한다 — 읽기는 파괴적이지 않다."""
    _make_session(sessions_root, VALID_ID, status="recording")

    stream, _ = open_archive(sessions_root, VALID_ID)
    stream.close()


# ─────────────────────────── 삭제 ───────────────────────────

def test_delete_removes_the_whole_session_directory(sessions_root: Path):
    _make_session(sessions_root, VALID_ID)
    _make_session(sessions_root, OTHER_ID)

    delete_session(sessions_root, VALID_ID, active_session_id=None)

    assert not (sessions_root / VALID_ID).exists()
    assert (sessions_root / OTHER_ID).exists()


def test_delete_refuses_the_session_that_is_recording(sessions_root: Path):
    _make_session(sessions_root, VALID_ID, status="recording")

    with pytest.raises(ArchiveConflictError):
        delete_session(sessions_root, VALID_ID, active_session_id=VALID_ID)

    assert (sessions_root / VALID_ID).exists()


def test_delete_of_a_missing_session_is_not_found(sessions_root: Path):
    with pytest.raises(ArchiveNotFoundError):
        delete_session(sessions_root, VALID_ID, active_session_id=None)
