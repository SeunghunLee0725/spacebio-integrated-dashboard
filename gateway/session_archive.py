"""기록된 세션의 목록·다운로드·삭제 — 파일시스템만 다룬다 (FastAPI 의존 없음).

`gateway/session_store.py`가 쓴 `<data_root>/sessions/<session_id>/`를 읽는다.
쓰기는 하지 않고, 지우는 것은 세션 디렉터리 통째뿐이다.

⚠ 삭제는 비가역이라 `session_id`를 두 번 검증한다 — 먼저 정규식으로, 그 다음
`resolve()`한 실제 경로가 sessions_root 바로 아래인지로. 심볼릭 링크로 루트
밖을 가리키는 디렉터리는 두 번째 검사에서 걸린다.
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Optional

from gateway.api_models import SESSION_ID_PATTERN

_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)

#: 센서 CSV 행 수를 세는 상한. 이보다 큰 파일은 세지 않고 None을 낸다 —
#: 목록 한 번 여는 데 수백 MB를 훑을 이유가 없다.
ROW_COUNT_MAX_BYTES = 64 * 1024 * 1024

#: 아카이브를 메모리에 들고 있을 상한. 넘으면 디스크로 흘린다.
SPOOL_MAX_BYTES = 8 * 1024 * 1024

_READ_CHUNK_BYTES = 1024 * 1024

SENSOR_CSV_NAME = "sensor_samples.csv"
MANIFEST_NAME = "manifest.json"


class ArchiveNotFoundError(LookupError):
    """그런 세션이 없다 — 경로 검증 실패도 여기로 모은다(존재 여부를 흘리지 않는다)."""


class ArchiveConflictError(Exception):
    """기록 중인 세션은 지울 수 없다."""

    code = "session_active"


@dataclass(frozen=True)
class ArchivedSession:
    session_id: str
    experiment_name: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]
    status: str
    sensor_mode: Optional[str]
    bytes: int
    sample_rows: Optional[int]
    active: bool = False


def list_sessions(
    sessions_root: Path, *, active_session_id: Optional[str] = None
) -> list[ArchivedSession]:
    """기록된 세션을 최신 먼저 나열한다. 루트가 없으면 빈 목록이다."""
    root = Path(sessions_root)
    if not root.is_dir():
        return []

    sessions = [
        _describe(entry, active=entry.name == active_session_id)
        for entry in root.iterdir()
        if entry.is_dir() and _SESSION_ID_RE.match(entry.name)
    ]
    # session_id에 날짜·시각이 들어 있어 이름 역순이 곧 최신 먼저다.
    return sorted(sessions, key=lambda s: s.session_id, reverse=True)


def open_archive(sessions_root: Path, session_id: str) -> tuple[IO[bytes], int]:
    """세션 디렉터리를 `.tar.gz`로 묶어 (스트림, 바이트 수)를 낸다.

    호출자가 스트림을 닫아야 한다. CSV는 5배 가까이 줄어든다.
    """
    directory = _session_dir(sessions_root, session_id)

    spool: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
    try:
        with tarfile.open(fileobj=spool, mode="w:gz") as tar:
            for path in sorted(directory.iterdir()):
                if path.is_file() and not path.is_symlink():
                    tar.add(path, arcname=f"{session_id}/{path.name}")
        size = spool.tell()
        spool.seek(0)
    except Exception:
        spool.close()
        raise
    return spool, size


def delete_session(
    sessions_root: Path, session_id: str, *, active_session_id: Optional[str]
) -> None:
    """세션 디렉터리를 통째로 지운다. 기록 중이면 거부한다."""
    directory = _session_dir(sessions_root, session_id)
    if active_session_id is not None and session_id == active_session_id:
        raise ArchiveConflictError(
            f"session {session_id} is still recording; stop the measurement first"
        )
    shutil.rmtree(directory)


def archive_filename(session_id: str) -> str:
    return f"{session_id}.tar.gz"


# ─────────────────────────── 내부 ───────────────────────────

def _session_dir(sessions_root: Path, session_id: str) -> Path:
    """`session_id`를 sessions_root 바로 아래의 실제 디렉터리로 바꾼다.

    정규식을 통과하더라도 resolve 결과가 루트 바로 아래가 아니면 거부한다 —
    심볼릭 링크로 루트 밖을 가리키는 경우가 여기서 걸린다.
    """
    if not _SESSION_ID_RE.match(session_id or ""):
        raise ArchiveNotFoundError(f"no such session: {session_id!r}")

    root = Path(sessions_root).resolve()
    directory = (root / session_id).resolve()
    if directory.parent != root or not directory.is_dir():
        raise ArchiveNotFoundError(f"no such session: {session_id!r}")
    return directory


def _describe(directory: Path, *, active: bool) -> ArchivedSession:
    manifest = _read_manifest(directory / MANIFEST_NAME)
    return ArchivedSession(
        session_id=directory.name,
        experiment_name=manifest.get("experiment_name"),
        started_at=manifest.get("started_at"),
        finished_at=manifest.get("finished_at"),
        status=str(manifest.get("status") or "unknown"),
        sensor_mode=manifest.get("sensor_mode"),
        bytes=_directory_bytes(directory),
        sample_rows=_count_sample_rows(directory / SENSOR_CSV_NAME),
        active=active,
    )


def _read_manifest(path: Path) -> dict:
    """manifest가 없거나 깨졌어도 목록에서 빠지면 안 된다 — 지울 수 없게 된다."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _directory_bytes(directory: Path) -> int:
    total = 0
    for path in directory.iterdir():
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _count_sample_rows(path: Path) -> Optional[int]:
    """CSV 데이터 행 수(헤더 제외). 파일이 너무 크면 세지 않고 None을 낸다."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0 or size > ROW_COUNT_MAX_BYTES:
        return None

    newlines = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK_BYTES):
                newlines += chunk.count(b"\n")
    except OSError:
        return None
    return max(newlines - 1, 0)  # 헤더 한 줄을 뺀다


__all__ = [
    "ArchiveConflictError",
    "ArchiveNotFoundError",
    "ArchivedSession",
    "archive_filename",
    "delete_session",
    "list_sessions",
    "open_archive",
]
