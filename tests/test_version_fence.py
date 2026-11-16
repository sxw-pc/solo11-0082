"""Version-fence scenarios: the ledger must never attribute one byte version
of the replay to another version's SHA-256."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from pointlog import pipeline as pipeline_module
from pointlog import replay as replay_module
from pointlog.contract import load_contract
from pointlog.pipeline import IngestInterrupted, PipelineError, run_capture
from pointlog.replay import ReplayError, sealed_copy_path
from pointlog.report_stats import collect_summary
from pointlog.resume import ResumeRejected
from pointlog.store import Store, make_batch_id
from pointlog.util import sha256_file


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path) -> str:
    return _sha_bytes(Path(path).read_bytes())


def _b_text() -> str:
    """A second replay version: different line count, different source tag."""
    lines = [
        {
            "ts": f"2025-06-01T00:00:{index:02d}Z",
            "request_hex": "010300000002c40b",
            "response_hex": "",
            "source": "gateway-b",
        }
        for index in range(7)
    ]
    return "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines)


B_TEXT = _b_text()


def _run(paths, **kwargs):
    return run_capture(
        replay_path=paths.fixture,
        contract_path=paths.contract,
        db_path=paths.db,
        reports_dir=None,
        **kwargs,
    )


def _partial_run(paths, stop_after):
    with pytest.raises(IngestInterrupted):
        _run(paths, stop_after=stop_after)


def _batch(db, batch_id):
    store = Store(db, read_only=True)
    try:
        return store.get_batch(batch_id)
    finally:
        store.close()


def _fence(db, batch_id):
    store = Store(db, read_only=True)
    try:
        return store.get_fence(batch_id)
    finally:
        store.close()


def _frame_sources(db, batch_id):
    store = Store(db, read_only=True)
    try:
        rows = store.conn.execute(
            "SELECT DISTINCT source FROM frames WHERE batch_id = ?", (batch_id,)
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        store.close()


def _frame_count(db, batch_id):
    store = Store(db, read_only=True)
    try:
        return store.conn.execute(
            "SELECT COUNT(*) FROM frames WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
    finally:
        store.close()


def _seal_dir(paths) -> Path:
    return paths.db.parent / "sealed"


def _force_writable(path: Path) -> None:
    os.chmod(path, 0o644)


# --- scenario 1: the source is replaced between the hash read and the rest ---


def test_replace_after_hash_is_fenced(demo_paths, monkeypatch):
    original = replay_module.sha256_file
    swapped = {"done": False}

    def swapping_sha256(path):
        digest = original(path)
        if not swapped["done"] and Path(path) == demo_paths.fixture:
            swapped["done"] = True
            demo_paths.fixture.write_text(B_TEXT, encoding="utf-8")
        return digest

    monkeypatch.setattr(replay_module, "sha256_file", swapping_sha256)
    with pytest.raises(ReplayError) as excinfo:
        _run(demo_paths)
    assert excinfo.value.code == "replay_unstable"
    assert swapped["done"]
    # No batch identity was derived from the moving source ...
    assert not demo_paths.db.exists()
    # ... and the fence did not "repair" or delete the source file.
    assert demo_paths.fixture.read_text(encoding="utf-8") == B_TEXT


# --- scenario 2: the source is replaced between the count read and the rest ---


def test_replace_after_count_is_fenced(demo_paths, monkeypatch):
    original = replay_module.count_lines

    def swapping_count(path):
        # Mirrors the reported reproduction: swap A for B right before the
        # real count happens.
        demo_paths.fixture.write_text(B_TEXT, encoding="utf-8")
        return original(path)

    monkeypatch.setattr(replay_module, "count_lines", swapping_count)
    with pytest.raises(ReplayError) as excinfo:
        _run(demo_paths)
    assert excinfo.value.code == "replay_unstable"
    assert not demo_paths.db.exists()
    assert demo_paths.fixture.read_text(encoding="utf-8") == B_TEXT


# --- scenario 3: the source is rewritten in place while lines are processed ---


def test_in_place_rewrite_during_processing_is_isolated(demo_paths, monkeypatch):
    a_bytes = demo_paths.fixture.read_bytes()
    # Same length, same line count, different bytes: neither length, line
    # count, mtime nor a prefix check could tell B apart from A here.
    b_bytes = a_bytes.decode("utf-8").replace("demo-gateway", "DEMO-gateway").encode("utf-8")
    assert len(a_bytes) == len(b_bytes)
    assert a_bytes.count(b"\n") == b_bytes.count(b"\n")
    sha_a = _sha_bytes(a_bytes)
    assert _sha_bytes(b_bytes) != sha_a

    real_process_line = pipeline_module.process_line
    calls = {"n": 0}

    def rewriting_process_line(store, batch_id, contract, line, seen_points):
        calls["n"] += 1
        if calls["n"] == 3:
            demo_paths.fixture.write_bytes(b_bytes)  # in-place rewrite mid-run
        return real_process_line(store, batch_id, contract, line, seen_points)

    monkeypatch.setattr(pipeline_module, "process_line", rewriting_process_line)
    result = _run(demo_paths)

    assert result.status == "completed"
    assert result.total_lines == 19
    batch = _batch(demo_paths.db, result.batch_id)
    assert batch["replay_sha256"] == sha_a
    # Every committed frame comes from version A, never from B.
    assert _frame_sources(demo_paths.db, result.batch_id) == {"demo-gateway"}
    fence = _fence(demo_paths.db, result.batch_id)
    assert fence["result"] == "verified"
    assert fence["source_status"] == "changed"  # drift detected by full-content hash
    assert fence["byte_count"] == len(a_bytes) == len(b_bytes)


# --- scenario 4: the source is replaced before an interrupted run resumes ---


def test_replace_before_resume_completes_from_sealed_copy(demo_paths):
    a_bytes = demo_paths.fixture.read_bytes()
    sha_a = _sha_bytes(a_bytes)
    _partial_run(demo_paths, 3)
    batch_id = make_batch_id(sha_a, sha256_file(demo_paths.contract))

    demo_paths.fixture.write_text(B_TEXT, encoding="utf-8")  # replaced by B
    result = _run(demo_paths, resume=True)

    assert result.batch_id == batch_id
    assert result.status == "completed"
    assert result.processed_lines == 19  # A's lines, not B's 7
    batch = _batch(demo_paths.db, batch_id)
    assert batch["replay_sha256"] == sha_a
    assert batch["total_lines"] == 19
    # B rows were never attributed to A's digest.
    assert _frame_sources(demo_paths.db, batch_id) == {"demo-gateway"}
    fence = _fence(demo_paths.db, batch_id)
    assert fence["result"] == "verified"
    assert fence["source_status"] == "changed"
    assert fence["byte_count"] == len(a_bytes)


# --- scenario 5: a normal large replay streams through the fence ---


def test_large_replay_streams_through_the_fence(demo_paths, tmp_path):
    # A multi-chunk replay (well over the 1 MiB streaming chunk) built from
    # few but long lines, so the run stays fast while every fingerprint and
    # verification pass still has to stream the file in bounded pieces.
    template = {
        "ts": "",
        "request_hex": "010300000002c40b",
        "response_hex": "01030400003034efe4",
        "source": "bulk-gateway",
        "pad": "x" * 1800,
    }
    line_count = 700
    with (tmp_path / "replay_big.jsonl").open("w", encoding="utf-8") as handle:
        for index in range(line_count):
            record = dict(template)
            record["ts"] = (
                f"2025-01-01T{index // 3600 % 24:02d}:"
                f"{index // 60 % 60:02d}:{index % 60:02d}Z"
            )
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    big = tmp_path / "replay_big.jsonl"
    assert big.stat().st_size > 1024 * 1024

    db = tmp_path / "big.db"
    result = run_capture(
        replay_path=big,
        contract_path=demo_paths.contract,
        db_path=db,
        reports_dir=None,
    )
    assert result.status == "completed"
    assert result.total_lines == line_count
    assert result.processed_lines == line_count
    assert result.reading_count == line_count
    assert _frame_count(db, result.batch_id) == line_count
    fence = _fence(db, result.batch_id)
    assert fence["result"] == "verified"
    assert fence["source_status"] == "match"
    assert fence["byte_count"] == big.stat().st_size
    assert fence["sha256"] == _sha_file(big)
    # The sealed evidence copy is retained and matches the processed version.
    sealed = Path(fence["sealed_path"])
    assert sealed.is_file()
    assert _sha_file(sealed) == fence["sha256"]


# --- scenario 6: old completed batches are not invalidated by fence failures ---


def test_completed_batches_survive_fence_failures(demo_paths, tmp_path):
    result_a = _run(demo_paths)
    assert result_a.status == "completed"
    fence_a_before = _fence(demo_paths.db, result_a.batch_id)

    # A second replay in the same ledger goes partial, then its version
    # becomes unprovable (sealed copy corrupted, source replaced).
    c_path = tmp_path / "replay_c.jsonl"
    c_text = B_TEXT.replace("gateway-b", "gateway-c")
    c_path.write_text(c_text, encoding="utf-8")
    sha_c = _sha_bytes(c_text.encode("utf-8"))
    with pytest.raises(IngestInterrupted):
        run_capture(
            replay_path=c_path,
            contract_path=demo_paths.contract,
            db_path=demo_paths.db,
            reports_dir=None,
            stop_after=2,
        )
    batch_c_id = make_batch_id(sha_c, sha256_file(demo_paths.contract))
    sealed_c = sealed_copy_path(_seal_dir(demo_paths), sha_c)
    _force_writable(sealed_c)
    sealed_c.write_bytes(b"corrupted\n")
    c_path.write_text(B_TEXT.replace("gateway-b", "gateway-d"), encoding="utf-8")

    with pytest.raises(ResumeRejected) as excinfo:
        run_capture(
            replay_path=c_path,
            contract_path=demo_paths.contract,
            db_path=demo_paths.db,
            reports_dir=None,
            resume=True,
        )
    assert excinfo.value.code == "version_unproven"

    # The old completed batch is untouched and still reusable.
    batch_a = _batch(demo_paths.db, result_a.batch_id)
    assert batch_a["status"] == "completed"
    assert _fence(demo_paths.db, result_a.batch_id) == fence_a_before
    again = _run(demo_paths)
    assert again.reused
    assert again.batch_id == result_a.batch_id
    assert again.reading_count == result_a.reading_count

    # The failed batch keeps its diagnostic frames but stays non-completed.
    batch_c = _batch(demo_paths.db, batch_c_id)
    assert batch_c["status"] == "partial"
    assert _frame_count(demo_paths.db, batch_c_id) == 2
    assert _fence(demo_paths.db, batch_c_id)["result"] == "unproven"


# --- version unprovable at resume: stats are marked unavailable ---


def test_unproven_version_keeps_frames_but_marks_stats_unavailable(demo_paths):
    a_bytes = demo_paths.fixture.read_bytes()
    sha_a = _sha_bytes(a_bytes)
    _partial_run(demo_paths, 3)
    batch_id = make_batch_id(sha_a, sha256_file(demo_paths.contract))

    # The sealed evidence is destroyed and the source path now holds B.
    sealed = sealed_copy_path(_seal_dir(demo_paths), sha_a)
    _force_writable(sealed)
    sealed.unlink()
    demo_paths.fixture.write_text(B_TEXT, encoding="utf-8")

    with pytest.raises(ResumeRejected) as excinfo:
        _run(demo_paths, resume=True)
    assert excinfo.value.code == "version_unproven"

    batch = _batch(demo_paths.db, batch_id)
    assert batch["status"] == "partial"  # never completed
    assert _frame_count(demo_paths.db, batch_id) == 3  # frames kept for diagnostics
    fence = _fence(demo_paths.db, batch_id)
    assert fence["result"] == "unproven"
    assert fence["source_status"] == "changed"
    # The source file itself was not deleted or rewritten by the fence.
    assert demo_paths.fixture.read_text(encoding="utf-8") == B_TEXT

    store = Store(demo_paths.db, read_only=True)
    try:
        summary = collect_summary(store, batch_id, load_contract(demo_paths.contract))
    finally:
        store.close()
    assert summary["version_fence"]["result"] == "unproven"
    assert summary["totals"]["stats_status"] == "unavailable"
    assert summary["totals"]["valid_readings"] is None
    assert summary["totals"]["pending_points"] is None
    assert summary["pending_recollect"] is None
    # Diagnostic frame/reading facts remain queryable.
    assert summary["totals"]["frames"] == 3
    assert summary["totals"]["readings"] == 3


# --- the sealed copy itself is tampered with mid-run: batch can never complete ---


def test_sealed_copy_tampered_mid_run_is_violated(demo_paths, monkeypatch):
    a_bytes = demo_paths.fixture.read_bytes()
    sha_a = _sha_bytes(a_bytes)
    batch_id = make_batch_id(sha_a, sha256_file(demo_paths.contract))
    sealed = sealed_copy_path(_seal_dir(demo_paths), sha_a)

    real_process_line = pipeline_module.process_line
    calls = {"n": 0}

    def tampering_process_line(store, batch_id_, contract, line, seen_points):
        calls["n"] += 1
        if calls["n"] == 2:
            _force_writable(sealed)
            sealed.write_bytes(b"")  # truncate the private copy mid-run
        return real_process_line(store, batch_id_, contract, line, seen_points)

    monkeypatch.setattr(pipeline_module, "process_line", tampering_process_line)
    with pytest.raises(PipelineError) as excinfo:
        _run(demo_paths)
    assert excinfo.value.code == "version_fence_violated"

    batch = _batch(demo_paths.db, batch_id)
    assert batch["status"] == "failed"  # not completed
    assert _frame_count(demo_paths.db, batch_id) >= 2  # frames kept for diagnostics
    fence = _fence(demo_paths.db, batch_id)
    assert fence["result"] == "violated"

    store = Store(demo_paths.db, read_only=True)
    try:
        summary = collect_summary(store, batch_id, load_contract(demo_paths.contract))
    finally:
        store.close()
    assert summary["totals"]["stats_status"] == "unavailable"
    assert summary["totals"]["valid_readings"] is None
    assert summary["pending_recollect"] is None

    # A violated batch is terminal: resume refuses even though the source is
    # intact again, and a plain rerun refuses the half-written identity.
    demo_paths.fixture.write_bytes(a_bytes)
    with pytest.raises(ResumeRejected) as excinfo:
        _run(demo_paths, resume=True)
    assert excinfo.value.code == "version_unproven"
    with pytest.raises(PipelineError) as excinfo:
        _run(demo_paths)
    assert excinfo.value.code == "batch_partial"


# --- recovery: sealed copy lost but the source still proves the version ---


def test_resume_reseals_when_source_still_matches(demo_paths):
    a_bytes = demo_paths.fixture.read_bytes()
    sha_a = _sha_bytes(a_bytes)
    _partial_run(demo_paths, 3)
    batch_id = make_batch_id(sha_a, sha256_file(demo_paths.contract))

    sealed = sealed_copy_path(_seal_dir(demo_paths), sha_a)
    _force_writable(sealed)
    sealed.unlink()  # evidence lost, but the source still holds version A

    result = _run(demo_paths, resume=True)
    assert result.batch_id == batch_id
    assert result.status == "completed"
    assert result.processed_lines == 19
    fence = _fence(demo_paths.db, batch_id)
    assert fence["result"] == "verified"
    assert fence["source_status"] == "match"
    assert Path(fence["sealed_path"]).is_file()  # re-sealed during resume


# --- streaming fingerprint primitives match count_lines semantics ---


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\n",
        b"a\nb\n",
        b"a\nb",  # final line without trailing newline still counts
        b"x" * (2 * 1024 * 1024 + 3),  # multi-chunk, no newline at all
        (b'{"k":1}\n' * 1000)[:-1],  # many lines, unterminated tail
    ],
)
def test_hash_count_bytes_matches_count_lines(tmp_path, data):
    target = tmp_path / "case.bin"
    target.write_bytes(data)
    sha, lines, nbytes = replay_module._hash_count_bytes(target)
    assert sha == _sha_bytes(data)
    assert lines == replay_module.count_lines(target)
    assert nbytes == len(data)
