"""SQLite ledger access with idempotent writes and line-level commits."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .schema import SCHEMA_SQL, SCHEMA_VERSION


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
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        # Databases created by earlier releases only need the new derived
        # tables added; the CREATE IF NOT EXISTS script above is the migration.
        stored = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if stored is None or int(stored[0]) < SCHEMA_VERSION:
            self.conn.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )
        self.conn.commit()

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
                    "engineering_value, unit, quality_code, enum_label, source) "
                    "VALUES(:batch_id,:device_id,:point_key,:ts,:line_no,:raw_value,"
                    ":engineering_value,:unit,:quality_code,:enum_label,:source)",
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

    # --- Derived-point ledger ----------------------------------------------

    def get_derived_contract(self, contract_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM derived_contracts WHERE id = ?", (contract_id,)
        ).fetchone()
        return self._dict(row)

    def find_derived_contract_by_sha(self, sha256: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM derived_contracts WHERE sha256 = ?", (sha256,)
        ).fetchone()
        return self._dict(row)

    def derived_point_definitions(self, contract_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM derived_points WHERE contract_id = ? ORDER BY ordinal",
            (contract_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def derived_reading_rows(
        self, batch_id: str, contract_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = ("SELECT * FROM derived_readings WHERE batch_id = ?"
               + (" AND contract_id = ?" if contract_id else "")
               + " ORDER BY device_id, point_key, ts, id")
        params: tuple[Any, ...] = (batch_id, contract_id) if contract_id else (batch_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def derived_row_counts(
        self, batch_id: str, contract_id: str
    ) -> dict[tuple[str, str], dict[str, int]]:
        counts: dict[tuple[str, str], dict[str, int]] = {}
        rows = self.conn.execute(
            "SELECT device_id, point_key, quality_code, COUNT(*) AS n "
            "FROM derived_readings WHERE batch_id = ? AND contract_id = ? "
            "GROUP BY device_id, point_key, quality_code",
            (batch_id, contract_id),
        ).fetchall()
        for row in rows:
            key = (row["device_id"], row["point_key"])
            bucket = counts.setdefault(key, {"total": 0, "good": 0, "indeterminate": 0})
            bucket["total"] += int(row["n"])
            if row["quality_code"] == "derived_good":
                bucket["good"] += int(row["n"])
            else:
                bucket["indeterminate"] += int(row["n"])
        return counts
