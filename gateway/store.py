"""Local persistence — SQLite for state/events (DEC-010).

Raw high-rate sensor streams stay in CSV/HDF5 via the existing
data_logger; this store keeps queryable events and status snapshots.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger("gateway.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sensor (
    ts INTEGER NOT NULL,
    tension_n REAL, delta_r_over_r0 REAL, temp_c REAL, battery_pct INTEGER
);
CREATE TABLE IF NOT EXISTS pump_event (
    ts INTEGER NOT NULL, action TEXT, source TEXT, volume_ul REAL, rate_ul_s REAL
);
CREATE TABLE IF NOT EXISTS loop_state (
    ts INTEGER NOT NULL, armed INTEGER, trigger TEXT, threshold REAL, interlock TEXT
);
CREATE TABLE IF NOT EXISTS command (
    ts INTEGER NOT NULL, topic TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_sensor_ts ON sensor(ts);
CREATE INDEX IF NOT EXISTS idx_pump_ts ON pump_event(ts);
"""


class Store:
    def __init__(self, db_path: str = "./data/gateway.sqlite") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def insert(self, table: str, row: dict[str, Any]) -> None:
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        try:
            self._conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                tuple(row.values()),
            )
            self._conn.commit()
        except sqlite3.Error:
            logger.exception("insert into %s failed", table)

    def close(self) -> None:
        self._conn.close()
