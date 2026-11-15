import pytest

from pointlog.contract import ContractError
from pointlog.pipeline import run_capture
from pointlog.quality import QUALITY_ORDER
from pointlog.replay import parse_line
from pointlog.store import Store
from pointlog.store_queries import quality_distribution


def test_demo_capture_end_to_end(demo_run):
    result = demo_run.result
    assert result.status == "completed"
    assert result.total_lines == 19
    assert result.processed_lines == 19
    assert result.reading_count == 11
    assert result.valid_readings == 8
    assert result.issue_count == 12
    assert result.pending_points == 7
    assert set(result.report_paths) == {"json", "markdown", "html", "csv"}

    store = Store(demo_run.paths.db, read_only=True)
    try:
        batch = store.get_batch(result.batch_id)
        assert batch["status"] == "completed"
        assert batch["total_lines"] == 19
        assert batch["processed_lines"] == 19
        assert batch["valid_readings"] == 8
        tables = {
            row[0] for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"batches", "frames", "readings", "issues", "resume_state"} <= tables
    finally:
        store.close()


def test_demo_quality_distribution(demo_run):
    store = Store(demo_run.paths.db, read_only=True)
    try:
        quality = quality_distribution(store, demo_run.result.batch_id)
    finally:
        store.close()
    assert quality == {
        "good": 8,
        "bad_frame": 3,
        "bad_crc_request": 1,
        "bad_crc_response": 1,
        "exception_response": 1,
        "no_contract": 1,
        "missing_response": 1,
        "duplicate": 1,
        "overflow": 1,
        "under_range": 1,
        "not_in_enum": 1,
    }
    assert list(quality) == [code for code in QUALITY_ORDER if code in quality]


def test_demo_issue_rows_and_duplicate_handling(demo_run):
    store = Store(demo_run.paths.db, read_only=True)
    try:
        batch_id = demo_run.result.batch_id
        missing = store.conn.execute(
            "SELECT * FROM issues WHERE batch_id=? AND point_key='line_a_energy'",
            (batch_id,),
        ).fetchall()
        assert len(missing) == 1
        assert missing[0]["line_no"] == 0
        assert missing[0]["pending_collect"] == 1
        no_contract = store.conn.execute(
            "SELECT * FROM frames WHERE batch_id=? AND quality_code='no_contract'",
            (batch_id,),
        ).fetchall()
        assert len(no_contract) == 1
        assert no_contract[0]["point_key"] == ""
        duplicate_frame = store.conn.execute(
            "SELECT * FROM frames WHERE batch_id=? AND line_no=19", (batch_id,)
        ).fetchone()
        assert duplicate_frame["quality_code"] == "duplicate"
        speed_rows = store.conn.execute(
            "SELECT COUNT(*) FROM readings WHERE batch_id=? AND point_key='line_a_speed'",
            (batch_id,),
        ).fetchone()[0]
        assert speed_rows == 2  # good baseline + explicit overflow, duplicate skipped
        for row in store.conn.execute(
            "SELECT * FROM issues WHERE batch_id=? AND line_no > 0", (batch_id,)
        ):
            assert row["frame_summary"]
    finally:
        store.close()


def test_rerun_is_idempotent(demo_run):
    paths = demo_run.paths
    first = demo_run.result
    store = Store(paths.db, read_only=True)
    try:
        before = {
            table: store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("batches", "frames", "readings", "issues")
        }
    finally:
        store.close()

    second = run_capture(
        replay_path=paths.fixture,
        contract_path=paths.contract,
        db_path=paths.db,
        reports_dir=paths.reports,
    )
    assert second.reused
    assert second.batch_id == first.batch_id
    assert second.reading_count == first.reading_count

    store = Store(paths.db, read_only=True)
    try:
        after = {
            table: store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("batches", "frames", "readings", "issues")
        }
    finally:
        store.close()
    assert after == before


def test_contract_error_does_not_create_database(demo_paths, tmp_path):
    broken = tmp_path / "broken.yaml"
    original = demo_paths.contract.read_text(encoding="utf-8")
    broken.write_text(original.replace("scale: 0.1", "scale: 0", 1), encoding="utf-8")
    db = tmp_path / "never.db"
    with pytest.raises(ContractError) as excinfo:
        run_capture(
            replay_path=demo_paths.fixture,
            contract_path=broken,
            db_path=db,
            reports_dir=None,
        )
    assert excinfo.value.code == "contract_scale"
    assert not db.exists()


def test_replay_line_parser_reports_bad_json():
    line = parse_line(3, '{"ts": "broken"')
    assert line.line_no == 3
    assert not line.valid
    assert line.error_code == "invalid_json"
    blank = parse_line(4, "\n")
    assert blank.error_code == "invalid_line"
