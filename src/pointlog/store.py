"""SQLite ledger access with idempotent writes and line-level commits."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .schema import SCHEMA_SQL, SCHEMA_VERSION
from .util import parse_utc_timestamp


def make_batch_id(replay_sha256: str, contract_sha256: str) -> str:
    return f"b_{replay_sha256[:12]}_{contract_sha256[:12]}"


class Store:
    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, timeout=10)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if not read_only:
            self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self._migrate_readings()
        self.conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _migrate_readings(self) -> None:
        """Add schema-v2 calibration columns to pre-existing ledgers."""
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(readings)")
        }
        with self.conn:
            if "calibration_version_id" not in columns:
                self.conn.execute(
                    "ALTER TABLE readings ADD COLUMN "
                    "calibration_version_id TEXT NOT NULL DEFAULT ''"
                )
            if "calibration_registered_at" not in columns:
                self.conn.execute(
                    "ALTER TABLE readings ADD COLUMN "
                    "calibration_registered_at TEXT NOT NULL DEFAULT ''"
                )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        return self._dict(row)

    def find_batch(self, replay_sha256: str, contract_sha256: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM batches WHERE replay_sha256 = ? AND contract_sha256 = ?",
            (replay_sha256, contract_sha256),
        ).fetchone()
        return self._dict(row)

    def find_latest_batch_for_path(self, replay_path: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM batches WHERE replay_path = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (replay_path,),
        ).fetchone()
        return self._dict(row)

    def create_batch(
        self,
        *,
        batch_id: str,
        replay_path: str,
        replay_sha256: str,
        contract_path: str,
        contract_sha256: str,
        started_at: str,
        total_lines: int,
    ) -> dict[str, Any]:
        self.conn.execute(
            "INSERT OR IGNORE INTO batches("
            "id, replay_path, replay_sha256, contract_path, contract_sha256, "
            "started_at, status, total_lines) VALUES(?,?,?,?,?,?,'partial',?)",
            (batch_id, replay_path, replay_sha256, contract_path, contract_sha256,
             started_at, total_lines),
        )
        self.conn.commit()
        batch = self.get_batch(batch_id)
        if batch is None:  # pragma: no cover - defensive
            raise RuntimeError("failed to create batch")
        return batch

    def ensure_resume_state(
        self,
        *,
        replay_path: str,
        replay_sha256: str,
        batch_id: str,
        total_lines: int,
        updated_at: str,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO resume_state("
            "replay_path, replay_sha256, batch_id, line_offset, total_lines, updated_at) "
            "VALUES(?,?,?,0,?,?)",
            (replay_path, replay_sha256, batch_id, total_lines, updated_at),
        )
        self.conn.commit()

    def set_resume_state(
        self,
        *,
        replay_path: str,
        replay_sha256: str,
        batch_id: str,
        line_offset: int,
        total_lines: int,
        updated_at: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO resume_state("
                "replay_path, replay_sha256, batch_id, line_offset, total_lines, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(replay_path, replay_sha256) DO UPDATE SET "
                "batch_id=excluded.batch_id, line_offset=excluded.line_offset, "
                "total_lines=excluded.total_lines, updated_at=excluded.updated_at",
                (replay_path, replay_sha256, batch_id, line_offset, total_lines, updated_at),
            )

    def get_resume_state(self, replay_path: str, replay_sha256: str):
        row = self.conn.execute(
            "SELECT * FROM resume_state WHERE replay_path = ? AND replay_sha256 = ?",
            (replay_path, replay_sha256),
        ).fetchone()
        return self._dict(row)

    def list_resume_states(self, replay_path: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM resume_state WHERE replay_path = ? "
            "ORDER BY updated_at DESC, replay_sha256",
            (replay_path,),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_line(
        self,
        *,
        batch_id: str,
        frame: dict[str, Any],
        issues: list[dict[str, Any]],
        reading: dict[str, Any] | None,
        state_key: tuple[str, str],
        line_offset: int,
        total_lines: int,
        updated_at: str,
    ) -> None:
        """Persist one replay line and its offset in one transaction."""
        replay_path, replay_sha256 = state_key
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO frames("
                "batch_id, line_no, ts, source, request_hex, response_hex, "
                "request_crc_ok, response_crc_ok, slave_id, function_code, "
                "start_address, register_count, device_id, point_key, "
                "contract_matched, quality_code, issue_code, detail) "
                "VALUES(:batch_id,:line_no,:ts,:source,:request_hex,:response_hex,"
                ":request_crc_ok,:response_crc_ok,:slave_id,:function_code,"
                ":start_address,:register_count,:device_id,:point_key,"
                ":contract_matched,:quality_code,:issue_code,:detail)",
                {"batch_id": batch_id, **frame},
            )
            for issue in issues:
                self.conn.execute(
                    "INSERT OR IGNORE INTO issues("
                    "batch_id, line_no, device_id, point_key, issue_code, "
                    "quality_code, severity, frame_summary, detail, ts, pending_collect) "
                    "VALUES(:batch_id,:line_no,:device_id,:point_key,:issue_code,"
                    ":quality_code,:severity,:frame_summary,:detail,:ts,:pending_collect)",
                    {"batch_id": batch_id, **issue},
                )
            if reading is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO readings("
                    "batch_id, device_id, point_key, ts, line_no, raw_value, "
                    "engineering_value, unit, quality_code, enum_label, source, "
                    "calibration_version_id, calibration_registered_at) "
                    "VALUES(:batch_id,:device_id,:point_key,:ts,:line_no,:raw_value,"
                    ":engineering_value,:unit,:quality_code,:enum_label,:source,"
                    ":calibration_version_id,:calibration_registered_at)",
                    {"batch_id": batch_id, **reading},
                )
            self.conn.execute(
                "UPDATE resume_state SET line_offset = ?, total_lines = ?, updated_at = ? "
                "WHERE replay_path = ? AND replay_sha256 = ?",
                (line_offset, total_lines, updated_at, replay_path, replay_sha256),
            )

    def add_issue(self, batch_id: str, issue: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO issues("
                "batch_id, line_no, device_id, point_key, issue_code, quality_code, "
                "severity, frame_summary, detail, ts, pending_collect) "
                "VALUES(:batch_id,:line_no,:device_id,:point_key,:issue_code,"
                ":quality_code,:severity,:frame_summary,:detail,:ts,:pending_collect)",
                {"batch_id": batch_id, **issue},
            )

    def reading_exists(self, batch_id: str, device_id: str, point_key: str, ts: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM readings WHERE batch_id = ? AND device_id = ? "
            "AND point_key = ? AND ts = ? LIMIT 1",
            (batch_id, device_id, point_key, ts),
        ).fetchone()
        return row is not None

    def seen_contract_points(self, batch_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT point_key FROM frames "
            "WHERE batch_id = ? AND contract_matched = 1",
            (batch_id,),
        ).fetchall()
        return {row["point_key"] for row in rows if row["point_key"]}

    def max_processed_line(self, batch_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(line_no), 0) AS n FROM frames WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        return int(row["n"])

    def refresh_batch_stats(
        self,
        batch_id: str,
        *,
        status: str,
        processed_lines: int,
        total_lines: int,
        finished_at: str | None,
    ) -> None:
        reading_count = self.conn.execute(
            "SELECT COUNT(*) FROM readings WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        valid_readings = self.conn.execute(
            "SELECT COUNT(*) FROM readings WHERE batch_id = ? AND quality_code = 'good'",
            (batch_id,),
        ).fetchone()[0]
        issue_count = self.conn.execute(
            "SELECT COUNT(*) FROM issues WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        with self.conn:
            self.conn.execute(
                "UPDATE batches SET status = ?, processed_lines = ?, reading_count = ?, "
                "valid_readings = ?, issue_count = ?, finished_at = ?, total_lines = ? "
                "WHERE id = ?",
                (status, processed_lines, reading_count, valid_readings, issue_count,
                 finished_at, total_lines, batch_id),
            )

    def update_status(self, batch_id: str, status: str, finished_at: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE batches SET status = ?, finished_at = ? WHERE id = ?",
                (status, finished_at, batch_id),
            )

    def find_latest_batch(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM batches ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return self._dict(row)

    def batch_readings(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM readings WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_derived_reading(self, batch_id: str, derived: dict[str, Any]) -> bool:
        """Append one derived conclusion; True when a new row was inserted.

        INSERT OR IGNORE on (batch, device, point, ts, new version) makes a
        repeated recalculation with the same revision idempotent, while a
        later revision covering the same interval adds a fresh row and keeps
        the earlier derived evidence intact.
        """
        with self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO derived_readings("
                "batch_id, device_id, point_key, ts, source_reading_id, "
                "old_calibration_version_id, new_calibration_version_id, "
                "new_calibration_registered_at, scale, offset, raw_value, "
                "engineering_value, quality_code, enum_label, input_sha256, created_at) "
                "VALUES(:batch_id,:device_id,:point_key,:ts,:source_reading_id,"
                ":old_calibration_version_id,:new_calibration_version_id,"
                ":new_calibration_registered_at,:scale,:offset,:raw_value,"
                ":engineering_value,:quality_code,:enum_label,:input_sha256,:created_at)",
                {"batch_id": batch_id, **derived},
            )
        return cursor.rowcount > 0

    def derived_readings(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM derived_readings WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_derived_map(self, batch_id: str) -> dict[tuple[str, str, str], dict[str, Any]]:
        """Map (device, point, ts) to the conclusion of the latest-registered
        revision, breaking ties by insertion order."""
        latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in self.derived_readings(batch_id):
            key = (row["device_id"], row["point_key"], row["ts"])
            current = latest.get(key)
            if current is None:
                latest[key] = row
                continue
            row_moment = parse_utc_timestamp(row["new_calibration_registered_at"])
            current_moment = parse_utc_timestamp(
                current["new_calibration_registered_at"]
            )
            row_key = (row_moment is not None, row_moment, row["id"])
            current_key = (
                current_moment is not None, current_moment, current["id"]
            )
            if row_key > current_key:
                latest[key] = row
        return latest
