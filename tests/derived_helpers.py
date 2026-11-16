"""Shared helpers for the offline virtual-point test suite."""

from __future__ import annotations

from pathlib import Path

from pointlog.contract import parse_contract
from pointlog.derived import parse_derived_config
from pointlog.store import Store
from pointlog.util import utc_now_iso

# A small two-device contract covering the units the derived tests exercise.
BASE_CONTRACT = """
version: 1
devices:
  - id: dev1
    label: Device One
    points:
      - {key: volt, slave_id: 1, function: 3, start: 0, count: 2, data_type: uint32,
         byte_order: ABCD, scale: 1, offset: 0, unit: V, range: [0, 1000]}
      - {key: millivolt, slave_id: 1, function: 3, start: 2, count: 2, data_type: uint32,
         byte_order: ABCD, scale: 1, offset: 0, unit: mV, range: [0, 1000000]}
      - {key: curr, slave_id: 1, function: 3, start: 4, count: 2, data_type: uint32,
         byte_order: ABCD, scale: 1, offset: 0, unit: A, range: [0, 100]}
      - {key: dur, slave_id: 1, function: 3, start: 6, count: 2, data_type: uint32,
         byte_order: ABCD, scale: 1, offset: 0, unit: s, range: [0, 86400]}
      - {key: temp, slave_id: 1, function: 4, start: 10, count: 1, data_type: int16,
         byte_order: AB, scale: 1, offset: 0, unit: degC, range: [-50, 200]}
      - {key: temp_b, slave_id: 1, function: 4, start: 11, count: 1, data_type: int16,
         byte_order: AB, scale: 1, offset: 0, unit: degC, range: [-50, 200]}
      - {key: rpos, slave_id: 1, function: 4, start: 12, count: 2, data_type: float32,
         byte_order: ABCD, scale: 1, offset: 0, unit: Ohm, range: [1, 1000]}
  - id: dev2
    label: Device Two
    points:
      - {key: other_v, slave_id: 2, function: 3, start: 0, count: 2, data_type: uint32,
         byte_order: ABCD, scale: 1, offset: 0, unit: V, range: [0, 1000]}
"""

POWER_DERIVED_GUARANTEED = """
version: 1
error_semantics: guaranteed
derived_points:
  - device_id: dev1
    key: power_w
    expression: v * i
    unit: W
    threshold: {direction: above, value: 23}
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
      i: {point: curr, abs_error: 0.2, unit: A}
"""

POWER_DERIVED_INDEPENDENT = """
version: 1
error_semantics: independent
derived_points:
  - device_id: dev1
    key: power_w
    expression: v * i
    unit: W
    threshold: {direction: above, value: 23}
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
      i: {point: curr, abs_error: 0.2, unit: A}
"""


def make_contract(text: str = BASE_CONTRACT):
    return parse_contract(text)


def make_derived(doc: str, contract=None):
    return parse_derived_config(doc, contract or make_contract())


def write_yaml(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class LedgerHarness:
    """In-memory-style SQLite ledger helper for derived-point tests."""

    def __init__(self, tmp_path: Path, batch_id: str = "b_test"):
        self.db_path = tmp_path / "derived.db"
        self.store = Store(self.db_path)
        batch = self.store.create_batch(
            batch_id=batch_id, replay_path=str(tmp_path / "replay.jsonl"),
            replay_sha256="a" * 64, contract_path=str(tmp_path / "contract.yaml"),
            contract_sha256="b" * 64, started_at=utc_now_iso(), total_lines=0,
        )
        self.batch_id = batch["id"]
        self._line = 0

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _next_line(self) -> int:
        self._line += 1
        return self._line

    def add_reading(self, point_key: str, ts: str, value: float, unit: str,
                    device_id: str = "dev1", quality: str = "good",
                    line_no: int | None = None):
        line = line_no or self._next_line()
        self.store.conn.execute(
            "INSERT INTO readings(batch_id, device_id, point_key, ts, line_no, "
            "raw_value, engineering_value, unit, quality_code, enum_label, source) "
            "VALUES(?,?,?,?,?,?,?,?,?,'','test')",
            (self.batch_id, device_id, point_key, ts, line, value, value, unit, quality),
        )
        self.store.conn.commit()
        return line

    def add_frame(self, point_key: str, ts: str, quality: str,
                  device_id: str = "dev1", line_no: int | None = None):
        line = line_no or self._next_line()
        self.store.conn.execute(
            "INSERT INTO frames(batch_id, line_no, ts, source, request_hex, "
            "response_hex, request_crc_ok, response_crc_ok, slave_id, function_code, "
            "start_address, register_count, device_id, point_key, contract_matched, "
            "quality_code, issue_code, detail) "
            "VALUES(?,?,?,'test','','',0,0,1,3,0,2,?,?,1,?,'','')",
            (self.batch_id, line, ts, device_id, point_key, quality),
        )
        self.store.conn.commit()
        return line

    def mark_completed(self):
        self.store.refresh_batch_stats(
            self.batch_id, status="completed", processed_lines=self._line,
            total_lines=self._line, finished_at=utc_now_iso(),
        )

    def materialize(self, config, source: str = "/tmp/derived.yaml"):
        from pointlog.derived import materialize_derived

        return materialize_derived(
            self.store, self.batch_id, make_contract(), config, source,
            now=utc_now_iso(),
        )

    def derived_rows(self, point_key: str | None = None):
        rows = self.store.derived_reading_rows(self.batch_id)
        if point_key is not None:
            rows = [row for row in rows if row["point_key"] == point_key]
        return rows

    def raw_reading_count(self) -> int:
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM readings WHERE batch_id = ?", (self.batch_id,)
        ).fetchone()[0]
