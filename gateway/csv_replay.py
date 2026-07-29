"""ThinkPad 실측 CSV 재생 (설계 스펙 6.3/6.7).

허용목록(manifest)에 등록된 dataset만 로드한다. header/타입/단조 timestamp
규칙을 어기는 행이 하나라도 있으면 configure 단계에서 파일 전체를 거부한다
(부분 로드 금지). 재생 스케줄링은 주입된 clock으로 결정되며, 테스트는 이
clock을 가짜로 주입해 실제 sleep 없이 검증한다.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from pydantic import TypeAdapter, ValidationError

from gateway.api_models import BatteryPct, SensorSample
from gateway.sensor_source import Clock, SensorSourceError

REQUIRED_COLUMNS = (
    "timestamp_ms", "raw_adc", "resistance_ohm",
    "delta_r_over_r0", "temperature_c", "battery_pct",
)
OPTIONAL_COLUMNS = ("elapsed_s",)
_ALLOWED_COLUMNS = frozenset(REQUIRED_COLUMNS) | frozenset(OPTIONAL_COLUMNS)

_battery_pct_adapter: TypeAdapter = TypeAdapter(BatteryPct)


@dataclass(frozen=True)
class CsvRow:
    """CSV 한 행의 검증된 값. `elapsed_s`는 보존하지 않는다(재생 시 재계산)."""

    timestamp_ms: int
    raw_adc: int
    resistance_ohm: float
    delta_r_over_r0: float
    temperature_c: float
    battery_pct: int


@dataclass(frozen=True)
class DatasetEntry:
    """`datasets/manifest.json`의 등록 항목 — allowlist 원본 (설계 스펙 6.7)."""

    dataset_id: str
    filename: str
    sha256: str
    provenance: str
    sample_count: int


def _validate_header(fieldnames: Optional[Sequence[str]]) -> None:
    if fieldnames is None:
        raise SensorSourceError("csv file has no header")
    columns = set(fieldnames)
    missing = set(REQUIRED_COLUMNS) - columns
    if missing:
        raise SensorSourceError(f"missing required columns: {sorted(missing)}")
    unknown = columns - _ALLOWED_COLUMNS
    if unknown:
        raise SensorSourceError(f"unknown columns: {sorted(unknown)}")


def _parse_row(fields: dict[str, str], line_no: int) -> CsvRow:
    # raw_adc는 CSV 검증 규칙에 range가 명시되지 않았다 — 센서 노드가
    # ADS1115(16비트 차동)라 raw_adc는 음수를 포함한 int16 전 범위를 쓴다
    # (ThinkPad 실측: -13263~12985). api_models.RawAdc가 이 범위를 반영하므로
    # 여기서는 정수 파싱만 하고 range는 SensorSample 생성 시 검증에 맡긴다.
    try:
        timestamp_ms = int(fields["timestamp_ms"])
        raw_adc = int(fields["raw_adc"])
        resistance_ohm = float(fields["resistance_ohm"])
        delta_r_over_r0 = float(fields["delta_r_over_r0"])
        temperature_c = float(fields["temperature_c"])
        battery_pct = _battery_pct_adapter.validate_python(int(fields["battery_pct"]))
    except (KeyError, ValueError, ValidationError) as exc:
        raise SensorSourceError(f"row {line_no}: invalid field: {exc}") from exc

    for name, value in (
        ("resistance_ohm", resistance_ohm),
        ("delta_r_over_r0", delta_r_over_r0),
        ("temperature_c", temperature_c),
    ):
        if not math.isfinite(value):
            raise SensorSourceError(f"row {line_no}: {name} must be finite")

    return CsvRow(
        timestamp_ms=timestamp_ms,
        raw_adc=raw_adc,
        resistance_ohm=resistance_ohm,
        delta_r_over_r0=delta_r_over_r0,
        temperature_c=temperature_c,
        battery_pct=battery_pct,
    )


def parse_csv_rows(path: Path) -> tuple[CsvRow, ...]:
    """CSV를 파싱하고 전체 검증한다. 잘못된 행이 하나라도 있으면 전체를 거부한다."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        _validate_header(reader.fieldnames)

        rows: list[CsvRow] = []
        previous_ts: Optional[int] = None
        for line_no, fields in enumerate(reader, start=2):
            if not any((value or "").strip() for value in fields.values()):
                continue  # 공백 행은 무시
            row = _parse_row(fields, line_no)
            if previous_ts is not None and row.timestamp_ms < previous_ts:
                raise SensorSourceError(
                    f"row {line_no}: timestamp_ms must be non-decreasing"
                )
            previous_ts = row.timestamp_ms
            rows.append(row)

    if not rows:
        raise SensorSourceError("csv file has no data rows")
    return tuple(rows)


def load_manifest(manifest_path: Path) -> dict[str, DatasetEntry]:
    """`datasets/manifest.json`을 읽어 dataset_id -> DatasetEntry 맵을 만든다."""
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries: dict[str, DatasetEntry] = {}
    for item in payload["datasets"]:
        entry = DatasetEntry(
            dataset_id=item["dataset_id"],
            filename=item["filename"],
            sha256=item["sha256"],
            provenance=item["provenance"],
            sample_count=item["sample_count"],
        )
        entries[entry.dataset_id] = entry
    return entries


def resolve_dataset_path(
    dataset_id: str, *, datasets_dir: Path, manifest: dict[str, DatasetEntry],
) -> Path:
    """dataset_id를 allowlist로 검증하고 datasets_dir 하위 경로로 resolve한다.

    경로 traversal과 SHA-256 무결성을 모두 확인한다 (설계 스펙 6.7).
    """
    entry = manifest.get(dataset_id)
    if entry is None:
        raise SensorSourceError(f"unknown dataset_id: {dataset_id!r}")

    root = datasets_dir.resolve()
    candidate = (datasets_dir / entry.filename).resolve()
    if not candidate.is_relative_to(root):
        raise SensorSourceError(f"dataset path escapes datasets_dir: {entry.filename!r}")
    if not candidate.is_file():
        raise SensorSourceError(f"dataset file not found: {candidate}")

    actual_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if actual_sha256 != entry.sha256:
        raise SensorSourceError(
            f"sha256 mismatch for dataset_id={dataset_id!r}: "
            f"expected {entry.sha256}, got {actual_sha256}"
        )
    return candidate


class CsvReplaySource:
    """등록된 CSV 데이터셋을 주입된 clock 기준으로 재생한다 (설계 스펙 6.7).

    매 tick은 clock을 한 번 읽어 다음 예정 샘플이 준비됐으면 정확히 1개를
    반환한다. EOF에서 loop=True면 index 0으로 즉시 복귀하며 loop_count를
    올리고, loop=False면 stopped 상태가 된다.
    """

    def __init__(
        self,
        rows: Sequence[CsvRow],
        *,
        replay_speed: float = 1.0,
        loop: bool = True,
        clock: Clock = time.monotonic,
    ) -> None:
        if not rows:
            raise SensorSourceError("cannot replay an empty dataset")
        self._rows = tuple(rows)
        first_ts = self._rows[0].timestamp_ms
        self._rebased_ts = tuple(row.timestamp_ms - first_ts for row in self._rows)
        self._span_ms = self._rebased_ts[-1]
        self._replay_speed = replay_speed
        self._loop = loop
        self._clock = clock

        self._start_ref: Optional[float] = None
        self._cursor = 0
        self._loop_count = 0
        self._cycle_offset_ms = 0
        self._stopped = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def loop_count(self) -> int:
        return self._loop_count

    def start(self) -> None:
        """index 0에서 결정적으로 (재)시작한다."""
        self._start_ref = self._clock()
        self._cursor = 0
        self._loop_count = 0
        self._cycle_offset_ms = 0
        self._stopped = False

    def tick(self) -> Optional[SensorSample]:
        if self._start_ref is None or self._stopped:
            return None

        virtual_ms = (self._clock() - self._start_ref) * 1000.0 * self._replay_speed
        scheduled_ms = self._cycle_offset_ms + self._rebased_ts[self._cursor]
        if virtual_ms < scheduled_ms:
            return None

        sample = self._sample_at(self._cursor, self._loop_count)
        if self._cursor == len(self._rows) - 1:
            if self._loop:
                self._cursor = 0
                self._loop_count += 1
                self._cycle_offset_ms += self._span_ms
            else:
                self._stopped = True
        else:
            self._cursor += 1
        return sample

    def _sample_at(self, index: int, loop_count: int) -> SensorSample:
        row = self._rows[index]
        return SensorSample(
            source_timestamp_ms=row.timestamp_ms,
            session_elapsed_ms=self._rebased_ts[index],
            loop_count=loop_count,
            raw_adc=row.raw_adc,
            resistance_ohm=row.resistance_ohm,
            delta_r_over_r0=row.delta_r_over_r0,
            temperature_c=row.temperature_c,
            battery_pct=row.battery_pct,
        )


def load_csv_replay_source(
    dataset_id: str,
    *,
    datasets_dir: Path,
    manifest_path: Path,
    replay_speed: float = 1.0,
    loop: bool = True,
    clock: Clock = time.monotonic,
) -> CsvReplaySource:
    """allowlist 검증 -> 파싱 -> 재생 소스 구성까지 한 번에 수행한다."""
    manifest = load_manifest(manifest_path)
    path = resolve_dataset_path(dataset_id, datasets_dir=datasets_dir, manifest=manifest)
    rows = parse_csv_rows(path)
    return CsvReplaySource(rows, replay_speed=replay_speed, loop=loop, clock=clock)
