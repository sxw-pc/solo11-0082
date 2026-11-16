"""End-to-end capture integration: replay -> readings -> virtual points.

Covers idempotent replay, resume continuation, out-of-order timestamps and the
guarantee that raw readings are never modified by derivation.
"""

from __future__ import annotations

import json

import pytest

from pointlog.codec import encode_raw
from pointlog.contract import parse_contract
from pointlog.derived import DERIVED_GOOD
from pointlog.modbus import build_request, build_response
from pointlog.pipeline import IngestInterrupted, run_capture, run_derived
from pointlog.store import Store

CONTRACT_TEXT = """
version: 1
devices:
  - id: dev1
    label: Device One
    points:
      - {key: volt, slave_id: 1, function: 3, start: 0, count: 2, data_type: uint32,
         byte_order: ABCD, scale: 1, offset: 0, unit: V, range: [0, 1000]}
      - {key: curr, slave_id: 1, function: 3, start: 2, count: 2, data_type: uint32,
         byte_order: ABCD, scale: 1, offset: 0, unit: A, range: [0, 100]}
"""

DERIVED_TEXT = """
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


def _frame(point, raw_value, ts):
    request = build_request(point.slave_id, point.function, point.start,
                            point.count).hex()
    payload = encode_raw(raw_value, point.data_type, point.byte_order)
    response = build_response(point.slave_id, point.function, payload).hex()
    return {"ts": ts, "request_hex": request, "response_hex": response,
            "source": "test-gateway"}


def _write_replay(path, records):
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n"
                for record in records),
        encoding="utf-8",
    )


@pytest.fixture
def paths(tmp_path):
    contract_path = tmp_path / "points.yaml"
    contract_path.write_text(CONTRACT_TEXT, encoding="utf-8")
    derived_path = tmp_path / "derived.yaml"
    derived_path.write_text(DERIVED_TEXT, encoding="utf-8")
    replay_path = tmp_path / "replay.jsonl"
    db_path = tmp_path / "ledger.db"
    reports_dir = tmp_path / "reports"
    contract = parse_contract(CONTRACT_TEXT)
    volt = contract.get("volt")
    curr = contract.get("curr")
    records = [
        _frame(volt, 10, "2025-03-01T00:00:00Z"),
        _frame(curr, 2, "2025-03-01T00:00:00Z"),
        _frame(volt, 12, "2025-03-01T00:00:05Z"),
        _frame(curr, 3, "2025-03-01T00:00:05Z"),
    ]
    _write_replay(replay_path, records)
    return type("P", (), {
        "contract": contract_path, "derived": derived_path,
        "replay": replay_path, "db": db_path, "reports": reports_dir,
    })()


def test_capture_produces_virtual_readings_with_spec_values(paths):
    result = run_capture(
        replay_path=paths.replay, contract_path=paths.contract,
        db_path=paths.db, reports_dir=paths.reports,
        derived_config_path=paths.derived,
    )
    assert result.derived_rows_written == 2
    assert result.derived_good == 2
    with Store(paths.db, read_only=True) as store:
        rows = store.derived_reading_rows(result.batch_id)
    first = next(row for row in rows if row["ts"].endswith("00:00Z"))
    assert first["engineering_value"] == 20.0
    assert (first["lower_bound"], first["upper_bound"]) == (16.2, 24.2)
    assert first["threshold_state"] == "possibly_above"
    second = next(row for row in rows if row["ts"].endswith("00:05Z"))
    assert second["engineering_value"] == 36.0
    assert second["threshold_state"] == "above"


def test_raw_readings_are_never_modified(paths):
    result = run_capture(
        replay_path=paths.replay, contract_path=paths.contract,
        db_path=paths.db, reports_dir=None, derived_config_path=paths.derived,
    )
    with Store(paths.db, read_only=True) as store:
        before = [dict(row) for row in store.conn.execute(
            "SELECT * FROM readings WHERE batch_id=? ORDER BY id",
            (result.batch_id,))]
    run_derived(
        batch_id=result.batch_id, contract_path=paths.contract,
        derived_config_path=paths.derived, db_path=paths.db,
    )
    with Store(paths.db, read_only=True) as store:
        after = [dict(row) for row in store.conn.execute(
            "SELECT * FROM readings WHERE batch_id=? ORDER BY id",
            (result.batch_id,))]
    assert before == after
    assert len(before) == 4


def test_repeated_derivation_writes_nothing_twice(paths):
    result = run_capture(
        replay_path=paths.replay, contract_path=paths.contract,
        db_path=paths.db, reports_dir=None, derived_config_path=paths.derived,
    )
    second = run_derived(
        batch_id=result.batch_id, contract_path=paths.contract,
        derived_config_path=paths.derived, db_path=paths.db,
    )
    assert second["rows_written"] == 0
    assert second["skipped_existing"] == 2
    third = run_derived(
        batch_id=result.batch_id, contract_path=paths.contract,
        derived_config_path=paths.derived, db_path=paths.db,
    )
    assert third["rows_written"] == 0
    with Store(paths.db, read_only=True) as store:
        count = store.conn.execute(
            "SELECT COUNT(*) FROM derived_readings WHERE batch_id=?",
            (result.batch_id,),
        ).fetchone()[0]
    assert count == 2


def test_full_capture_replay_is_idempotent(paths):
    first = run_capture(
        replay_path=paths.replay, contract_path=paths.contract,
        db_path=paths.db, reports_dir=None, derived_config_path=paths.derived,
    )
    second = run_capture(
        replay_path=paths.replay, contract_path=paths.contract,
        db_path=paths.db, reports_dir=None, derived_config_path=paths.derived,
    )
    assert second.reused
    assert second.derived_rows_written == 0
    assert second.derived_skipped == 2
    assert second.batch_id == first.batch_id


def test_resume_then_derivation_completes_the_chain(paths):
    with pytest.raises(IngestInterrupted):
        run_capture(
            replay_path=paths.replay, contract_path=paths.contract,
            db_path=paths.db, reports_dir=None, stop_after=2,
            derived_config_path=paths.derived,
        )
    # No derived rows while the batch is still partial.
    with Store(paths.db) as store:
        assert store.conn.execute(
            "SELECT COUNT(*) FROM derived_readings").fetchone()[0] == 0
    resumed = run_capture(
        replay_path=paths.replay, contract_path=paths.contract,
        db_path=paths.db, reports_dir=paths.reports, resume=True,
        derived_config_path=paths.derived,
    )
    assert resumed.status == "completed"
    assert resumed.derived_good == 2
    with Store(paths.db, read_only=True) as store:
        distinct = store.conn.execute(
            "SELECT COUNT(DISTINCT device_id || char(31) || point_key || char(31) || ts) "
            "FROM derived_readings").fetchone()[0]
        total = store.conn.execute(
            "SELECT COUNT(*) FROM derived_readings").fetchone()[0]
    assert distinct == total == 2


def test_out_of_order_replay_still_aligns_by_timestamp(paths):
    contract = parse_contract(CONTRACT_TEXT)
    volt = contract.get("volt")
    curr = contract.get("curr")
    # Interleave timestamps so matching pairs are never adjacent lines.
    records = [
        _frame(volt, 10, "2025-03-01T00:00:10Z"),
        _frame(volt, 10, "2025-03-01T00:00:00Z"),
        _frame(curr, 2, "2025-03-01T00:00:10Z"),
        _frame(curr, 2, "2025-03-01T00:00:00Z"),
        _frame(volt, 10, "2025-03-01T00:00:05Z"),
        _frame(curr, 2, "2025-03-01T00:00:05Z"),
    ]
    _write_replay(paths.replay, records)
    result = run_capture(
        replay_path=paths.replay, contract_path=paths.contract,
        db_path=paths.db, reports_dir=None, derived_config_path=paths.derived,
    )
    assert result.derived_good == 3
    with Store(paths.db, read_only=True) as store:
        rows = store.derived_reading_rows(result.batch_id)
    assert len(rows) == 3
    assert {row["ts"] for row in rows} == {
        "2025-03-01T00:00:00Z", "2025-03-01T00:00:05Z", "2025-03-01T00:00:10Z"}
    for row in rows:
        assert row["quality_code"] == DERIVED_GOOD


def test_run_derived_rejects_non_completed_batch(paths):
    with pytest.raises(IngestInterrupted):
        run_capture(
            replay_path=paths.replay, contract_path=paths.contract,
            db_path=paths.db, reports_dir=None, stop_after=1,
        )
    from pointlog.pipeline import PipelineError

    with Store(paths.db, read_only=True) as store:
        batch_id = store.conn.execute(
            "SELECT id FROM batches").fetchone()[0]
    with pytest.raises(PipelineError) as excinfo:
        run_derived(
            batch_id=batch_id, contract_path=paths.contract,
            derived_config_path=paths.derived, db_path=paths.db,
        )
    assert excinfo.value.code == "batch_not_completed"


def test_broken_derived_contract_fails_before_database_writes(paths):
    from pointlog.derived import DerivedConfigError

    broken = paths.contract.parent / "broken_derived.yaml"
    broken.write_text(DERIVED_TEXT.replace("unit: W", "unit: V"), encoding="utf-8")
    with pytest.raises(DerivedConfigError) as excinfo:
        run_capture(
            replay_path=paths.replay, contract_path=paths.contract,
            db_path=paths.db, reports_dir=None, derived_config_path=broken,
        )
    assert excinfo.value.code == "derived_output_unit"
    with Store(paths.db) as store:
        assert store.conn.execute(
            "SELECT COUNT(*) FROM derived_readings").fetchone()[0] == 0


def test_reports_carry_semantics_and_derived_section(paths):
    run_capture(
        replay_path=paths.replay, contract_path=paths.contract,
        db_path=paths.db, reports_dir=paths.reports,
        derived_config_path=paths.derived,
    )
    summary = json.loads((paths.reports / "summary.json").read_text(encoding="utf-8"))
    derived = summary["derived"]
    assert derived["error_semantics"] == "guaranteed"
    assert "holds for every allowed input" in derived["semantics_statement"]
    assert derived["independent_has_no_guarantee"] is False
    markdown = (paths.reports / "summary.md").read_text(encoding="utf-8")
    assert "## 虚拟测点（离线派生）" in markdown
    assert "possibly_above" in markdown
    html = (paths.reports / "summary.html").read_text(encoding="utf-8")
    assert "虚拟测点（离线派生）" in html
    csv_text = (paths.reports / "derived_readings.csv").read_text(encoding="utf-8")
    assert "power_w" in csv_text and "guaranteed" in csv_text


def test_independent_report_carries_no_guarantee_warning(paths):
    independent = paths.contract.parent / "independent.yaml"
    independent.write_text(
        DERIVED_TEXT.replace("error_semantics: guaranteed",
                             "error_semantics: independent"),
        encoding="utf-8",
    )
    run_capture(
        replay_path=paths.replay, contract_path=paths.contract,
        db_path=paths.db, reports_dir=paths.reports,
        derived_config_path=independent,
    )
    summary = json.loads((paths.reports / "summary.json").read_text(encoding="utf-8"))
    derived = summary["derived"]
    assert derived["error_semantics"] == "independent"
    assert derived["independent_has_no_guarantee"] is True
    assert "not a guarantee" in derived["semantics_statement"]
    markdown = (paths.reports / "summary.md").read_text(encoding="utf-8")
    assert "不保证" in markdown
