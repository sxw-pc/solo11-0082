import pytest

from pointlog.pipeline import IngestInterrupted, run_capture
from pointlog.resume import ResumeRejected
from pointlog.store import Store


def _partial_run(paths, stop_after):
    with pytest.raises(IngestInterrupted) as excinfo:
        run_capture(
            replay_path=paths.fixture,
            contract_path=paths.contract,
            db_path=paths.db,
            reports_dir=None,
            stop_after=stop_after,
        )
    return excinfo.value.batch_id


def test_resume_continues_without_duplicates(demo_paths):
    batch_id = _partial_run(demo_paths, 5)
    store = Store(demo_paths.db, read_only=True)
    try:
        batch = store.get_batch(batch_id)
        assert batch["status"] == "partial"
        assert batch["processed_lines"] == 5
        state = store.get_resume_state(
            str(demo_paths.fixture.resolve()), batch["replay_sha256"]
        )
        assert state["line_offset"] == 5
        assert store.conn.execute(
            "SELECT COUNT(*) FROM readings WHERE batch_id=?", (batch_id,)
        ).fetchone()[0] == 5
    finally:
        store.close()

    result = run_capture(
        replay_path=demo_paths.fixture,
        contract_path=demo_paths.contract,
        db_path=demo_paths.db,
        reports_dir=demo_paths.reports,
        resume=True,
    )
    assert result.batch_id == batch_id
    assert result.resumed
    assert result.status == "completed"
    assert result.processed_lines == 19
    assert result.reading_count == 11
    assert result.valid_readings == 8

    store = Store(demo_paths.db, read_only=True)
    try:
        total, distinct = store.conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT device_id || char(31) || point_key || "
            "char(31) || ts) FROM readings WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        assert total == distinct == 11
        batch_row = store.get_batch(batch_id)
        state = store.get_resume_state(
            str(demo_paths.fixture.resolve()), batch_row["replay_sha256"]
        )
        assert state["line_offset"] == 19
        assert store.conn.execute(
            "SELECT COUNT(*) FROM batches"
        ).fetchone()[0] == 1
    finally:
        store.close()

    again = run_capture(
        replay_path=demo_paths.fixture,
        contract_path=demo_paths.contract,
        db_path=demo_paths.db,
        reports_dir=None,
        resume=True,
    )
    assert again.reused
    assert again.reading_count == 11


def test_resume_after_resume_state_is_deleted(demo_paths):
    batch_id = _partial_run(demo_paths, 6)
    store = Store(demo_paths.db)
    try:
        store.conn.execute("DELETE FROM resume_state")
        store.conn.commit()
        assert store.conn.execute("SELECT COUNT(*) FROM resume_state").fetchone()[0] == 0
    finally:
        store.close()

    result = run_capture(
        replay_path=demo_paths.fixture,
        contract_path=demo_paths.contract,
        db_path=demo_paths.db,
        reports_dir=None,
        resume=True,
    )
    assert result.batch_id == batch_id
    assert result.status == "completed"
    assert result.reading_count == 11

    store = Store(demo_paths.db, read_only=True)
    try:
        state = store.conn.execute("SELECT * FROM resume_state").fetchone()
        assert state["line_offset"] == 19
        assert store.conn.execute(
            "SELECT COUNT(*) FROM readings WHERE batch_id=?", (batch_id,)
        ).fetchone()[0] == 11
    finally:
        store.close()


def test_resume_completes_from_sealed_copy_when_source_changes(demo_paths):
    """Sealed-fence route: replacing the source path after a partial run does
    not block the resume — the batch owns a verified private copy of the
    original version and completes from it.  The drift is reported through
    the fence record instead of being silently attributed."""
    batch_id = _partial_run(demo_paths, 4)
    original_batches = 1
    with demo_paths.fixture.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    result = run_capture(
        replay_path=demo_paths.fixture,
        contract_path=demo_paths.contract,
        db_path=demo_paths.db,
        reports_dir=None,
        resume=True,
    )
    assert result.batch_id == batch_id
    assert result.status == "completed"
    assert result.processed_lines == 19
    store = Store(demo_paths.db, read_only=True)
    try:
        assert store.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == original_batches
        batch = store.get_batch(batch_id)
        assert batch["status"] == "completed"
        fence = store.get_fence(batch_id)
        assert fence["result"] == "verified"
        assert fence["source_status"] == "changed"
    finally:
        store.close()


def test_resume_without_any_state_is_rejected(demo_paths):
    with pytest.raises(ResumeRejected) as excinfo:
        run_capture(
            replay_path=demo_paths.fixture,
            contract_path=demo_paths.contract,
            db_path=demo_paths.db,
            reports_dir=None,
            resume=True,
        )
    assert excinfo.value.code == "no_resume_state"
    store = Store(demo_paths.db, read_only=True)
    try:
        assert store.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    finally:
        store.close()
