"""Tests for shortest reproducible sub-replay search (pointlog.min_replay)."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from pointlog.min_replay import (
    DEVICE_MODE,
    TARGET_MODE,
    MinReplayError,
    MinReplayLimitError,
    TargetSpec,
    find_min_replay,
    write_min_replay_bundle,
)
from pointlog.modbus import (
    build_exception_response,
    build_request,
    build_response,
    corrupt_crc,
)
from pointlog.codec import encode_raw
from pointlog.pipeline import run_capture
from pointlog.store import Store

T1 = "2025-01-01T00:00:00Z"
T2 = "2025-01-01T00:00:05Z"
T3 = "2025-01-01T00:00:10Z"
T4 = "2025-01-01T00:00:15Z"
T5 = "2025-01-01T00:00:20Z"


def _jsonl(ts, request_hex, response_hex, source="gw"):
    return json.dumps(
        {"ts": ts, "request_hex": request_hex,
         "response_hex": response_hex, "source": source},
        separators=(",", ":"),
    ) + "\n"


def make_contract(path, keys, device="dev1", slave=1, function=3, rng=(0, 100)):
    blocks = []
    for index, key in enumerate(keys):
        blocks.append(
            f"""      - key: {key}
        slave_id: {slave}
        function: {function}
        start: {index}
        count: 1
        data_type: uint16
        byte_order: AB
        scale: 1
        offset: 0
        unit: ""
        range: [{rng[0]}, {rng[1]}]
"""
        )
    path.write_text(
        "version: 1\ndevices:\n"
        f"  - id: {device}\n    label: {device}\n    points:\n"
        + "".join(blocks),
        encoding="utf-8",
    )


def good_line(ts, start, value=10, *, slave=1, function=3, source="gw"):
    request_hex = build_request(slave, function, start, 1).hex()
    response_hex = build_response(
        slave, function, encode_raw(value, "uint16", "AB")
    ).hex()
    return _jsonl(ts, request_hex, response_hex, source)


def run_batch(tmp_path, raw_lines, keys, *, name="replay.jsonl", **contract_kwargs):
    contract = tmp_path / f"{name}.points.yaml"
    make_contract(contract, keys, **contract_kwargs)
    replay = tmp_path / name
    replay.write_text("".join(raw_lines), encoding="utf-8")
    db = tmp_path / f"{name}.db"
    result = run_capture(
        replay_path=replay, contract_path=contract,
        db_path=db, reports_dir=None,
    )
    return SimpleNamespace(
        contract=contract, replay=replay, db=db, batch_id=result.batch_id
    )


def target_for(ns, **where):
    store = Store(ns.db, read_only=True)
    try:
        clauses = " AND ".join(f"{key} = ?" for key in where)
        row = store.conn.execute(
            f"SELECT * FROM issues WHERE {clauses} LIMIT 1",
            tuple(where.values()),
        ).fetchone()
        assert row is not None, f"no issue matching {where}"
        return TargetSpec.from_row(dict(row))
    finally:
        store.close()


def find(ns, targets, mode=TARGET_MODE, **kwargs):
    return find_min_replay(
        batch_db_path=ns.db, batch_id=ns.batch_id,
        targets=tuple(targets), mode=mode, **kwargs,
    )


def sha256_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_duplicate_needs_prior_reading_target_mode(tmp_path):
    # Line 1: good P @t1; line 2: duplicate P @t1; line 3: bad CRC on Q.
    bad_crc_q = corrupt_crc(
        bytes.fromhex(good_line(T3, 1).split('"response_hex":"', 1)[1]
                      .split('"', 1)[0])
    ).hex()
    line3 = _jsonl(T3, build_request(1, 3, 1, 1).hex(), bad_crc_q)
    ns = run_batch(
        tmp_path,
        [good_line(T1, 0), good_line(T1, 0), line3],
        ["P", "Q", "R"],
    )
    target = target_for(ns, issue_code="duplicate", point_key="P")

    result = find(ns, [target])
    assert result.status == "optimal"
    assert result.minimality["winning_original_lines"] == [1, 2]
    assert result.sub_line_count == 2
    assert result.line_mapping == [
        {"sub_line_no": 1, "original_line_no": 1},
        {"sub_line_no": 2, "original_line_no": 2},
    ]
    present = result.target_present[0]
    assert present["point_key"] == "P"
    assert present["issue_code"] == "duplicate"
    assert present["quality_code"] == "duplicate"
    assert present["ts"] == T1
    assert present["sub_line_no"] == 2 and not present["synthetic"]

    # Deleting the first reading leaves a plain good frame: it must not be
    # called a duplicate.
    lone = tmp_path / "lone.jsonl"
    lone.write_text(ns.replay.read_text(encoding="utf-8").splitlines(keepends=True)[1])
    lone_db = tmp_path / "lone.db"
    run_capture(replay_path=lone, contract_path=ns.contract,
                db_path=lone_db, reports_dir=None)
    store = Store(lone_db, read_only=True)
    try:
        dup_rows = store.conn.execute(
            "SELECT COUNT(*) FROM issues WHERE issue_code='duplicate'"
        ).fetchone()[0]
        good_frames = store.conn.execute(
            "SELECT COUNT(*) FROM frames WHERE quality_code='good'"
        ).fetchone()[0]
    finally:
        store.close()
    assert dup_rows == 0 and good_frames == 1

    # Q was dropped by the subset: its line-0 missing response is induced by
    # subsetting and listed separately, not presented as a frame defect.
    induced = result.synthetic_missing["induced_by_subset"]
    assert [(row["point_key"], row["issue_code"]) for row in induced] == [
        ("Q", "missing_response")
    ]
    assert all(row["ts"] == "" for row in induced)
    # R was already absent in the original batch.
    preexisting = result.synthetic_missing["preexisting_from_original_batch"]
    assert [row["point_key"] for row in preexisting] == ["R"]


def test_device_mode_keeps_full_device_picture(tmp_path):
    bad_crc_q = corrupt_crc(
        bytes.fromhex(good_line(T3, 1).split('"response_hex":"', 1)[1]
                      .split('"', 1)[0])
    ).hex()
    line3 = _jsonl(T3, build_request(1, 3, 1, 1).hex(), bad_crc_q)
    ns = run_batch(
        tmp_path,
        [good_line(T1, 0), good_line(T1, 0), line3],
        ["P", "Q", "R"],
    )
    target = target_for(ns, issue_code="duplicate", point_key="P")

    target_result = find(ns, [target], mode=TARGET_MODE)
    device_result = find(ns, [target], mode=DEVICE_MODE)

    # Two complete routes have different shortest lengths and contexts.
    assert target_result.sub_line_count == 2
    assert device_result.sub_line_count == 3
    assert device_result.minimality["winning_original_lines"] == [1, 2, 3]

    # In device mode the bad-CRC Q frame is retained and still judged bad_crc.
    present_q = [
        row for row in device_result.report_diff["points"]["removed"]
        if row["point_key"] == "Q"
    ]
    assert present_q == []
    induced = device_result.synthetic_missing["induced_by_subset"]
    assert induced == []  # Q keeps its frame; no new synthetic issue for it
    preexisting = device_result.synthetic_missing["preexisting_from_original_batch"]
    assert [row["point_key"] for row in preexisting] == ["R"]

    # The target identity is still present in the device-wide sub-replay.
    assert device_result.target_present[0]["issue_code"] == "duplicate"


def test_device_mode_rejects_subset_that_induces_device_missing(tmp_path):
    # Device dev1: one good P read and one bad-CRC Q frame. A target-only
    # search for the Q issue drops P and induces a synthetic line-0 missing
    # for P; device mode must keep P's frame so the device issue picture is
    # identical to the original batch.
    bad_crc_q = corrupt_crc(
        bytes.fromhex(good_line(T3, 1).split('"response_hex":"', 1)[1]
                      .split('"', 1)[0])
    ).hex()
    line2 = _jsonl(T3, build_request(1, 3, 1, 1).hex(), bad_crc_q)
    ns = run_batch(
        tmp_path,
        [good_line(T1, 0), line2],
        ["P", "Q", "R"],
    )
    target = target_for(ns, issue_code="bad_crc_response", point_key="Q")

    target_result = find(ns, [target], mode=TARGET_MODE)
    device_result = find(ns, [target], mode=DEVICE_MODE)

    # Target mode keeps only the Q frame; dropping P induces its synthetic
    # missing_response, which is listed separately rather than treated as a
    # physical-frame problem.
    assert target_result.minimality["winning_original_lines"] == [2]
    induced_points = {
        row["point_key"]
        for row in target_result.synthetic_missing["induced_by_subset"]
    }
    assert "P" in induced_points

    # Device mode keeps both lines and induces no new synthetic missing.
    assert device_result.minimality["winning_original_lines"] == [1, 2]
    assert device_result.synthetic_missing["induced_by_subset"] == []
    # R was already missing in the original batch: still preexisting.
    assert [
        row["point_key"]
        for row in device_result.synthetic_missing["preexisting_from_original_batch"]
    ] == ["R"]
    # The device section of the report diff reports no device-level change.
    assert device_result.report_diff["devices"]["changed"] == []
    assert device_result.report_diff["devices"]["added"] == []
    assert device_result.report_diff["devices"]["removed"] == []


def test_synthetic_missing_target_reproduces_with_empty_subset(tmp_path):
    ns = run_batch(tmp_path, [good_line(T1, 0)], ["P", "Q"])
    target = target_for(
        ns, issue_code="missing_response", point_key="Q", line_no=0
    )
    result = find(ns, [target])
    assert result.sub_line_count == 0
    assert result.line_mapping == []
    assert result.minimality["winning_original_lines"] == []
    present = result.target_present[0]
    assert present["synthetic"] is True
    assert present["sub_line_no"] == 0
    assert present["point_key"] == "Q"

    bundle = write_min_replay_bundle(result, tmp_path / "empty_bundle")
    sub_bytes = (tmp_path / "empty_bundle" / "sub_replay.jsonl").read_bytes()
    assert sub_bytes == b""
    assert bundle["sub_replay_sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize(
    ("issue_code", "quality_code", "build_line"),
    [
        pytest.param(
            "bad_crc_request", "bad_crc_request",
            lambda: _jsonl(
                T2,
                corrupt_crc(bytes.fromhex(
                    build_request(1, 3, 1, 1).hex())).hex(),
                good_line(T2, 1).split('"response_hex":"', 1)[1].split('"', 1)[0],
            ),
            id="bad_crc_request",
        ),
        pytest.param(
            "bad_crc_response", "bad_crc_response",
            lambda: _jsonl(
                T2, build_request(1, 3, 1, 1).hex(),
                corrupt_crc(bytes.fromhex(
                    good_line(T2, 1).split('"response_hex":"', 1)[1]
                    .split('"', 1)[0])).hex(),
            ),
            id="bad_crc_response",
        ),
        pytest.param(
            "exception_response", "exception_response",
            lambda: _jsonl(
                T2, build_request(1, 3, 1, 1).hex(),
                build_exception_response(1, 3, 2).hex(),
            ),
            id="exception_response",
        ),
        pytest.param(
            "truncated_frame", "bad_frame",
            lambda: _jsonl(
                T2, build_request(1, 3, 1, 1).hex(),
                good_line(T2, 1).split('"response_hex":"', 1)[1]
                .split('"', 1)[0][:-6],
            ),
            id="bad_frame_truncated",
        ),
    ],
)
def test_single_line_minimum_preserves_pipeline_quality(
    tmp_path, issue_code, quality_code, build_line
):
    ns = run_batch(
        tmp_path, [good_line(T1, 0), build_line()], ["P", "Q"]
    )
    target = target_for(ns, issue_code=issue_code, quality_code=quality_code)
    result = find(ns, [target])
    assert result.minimality["winning_original_lines"] == [2]
    assert result.sub_line_count == 1
    present = result.target_present[0]
    assert present["issue_code"] == issue_code
    assert present["quality_code"] == quality_code
    assert present["point_key"] == "Q"


def test_bad_crc_priority_survives_subsetting_with_overflow(tmp_path):
    # A frame that would overflow on value is still bad_crc_response: the
    # original pipeline's priority must be reproduced inside the sub-replay.
    overflow_bad_crc = _jsonl(
        T2, build_request(1, 3, 1, 1).hex(),
        corrupt_crc(bytes.fromhex(
            good_line(T2, 1, value=999).split('"response_hex":"', 1)[1]
            .split('"', 1)[0]
        )).hex(),
    )
    ns = run_batch(
        tmp_path, [good_line(T1, 0), overflow_bad_crc], ["P", "Q"]
    )
    target = target_for(ns, issue_code="bad_crc_response")
    result = find(ns, [target])
    assert result.minimality["winning_original_lines"] == [2]
    present = result.target_present[0]
    assert present["quality_code"] == "bad_crc_response"
    assert present["issue_code"] == "bad_crc_response"
    # No overflow issue exists for the sub-replayed Q frame.
    assert not any(
        row["point_key"] == "Q" and row["issue_code"] == "overflow"
        for row in result.report_diff["points"]["removed"]
    )


def test_no_contract_identity_uses_empty_device_and_point(tmp_path):
    no_contract = _jsonl(
        T2,
        build_request(9, 3, 70, 1).hex(),
        build_response(9, 3, encode_raw(1, "uint16", "AB")).hex(),
    )
    ns = run_batch(tmp_path, [good_line(T1, 0), no_contract], ["P"])
    target = target_for(ns, issue_code="no_contract", device_id="", point_key="")
    result = find(ns, [target])
    assert result.minimality["winning_original_lines"] == [2]
    present = result.target_present[0]
    assert present["device_id"] == "" and present["point_key"] == ""
    assert present["quality_code"] == "no_contract"


def test_same_code_different_point_does_not_count_as_reproduction(tmp_path):
    # Both points overflow with the same issue/quality code; only P is target.
    line_q = good_line(T1, 1, value=999)
    line_p = good_line(T2, 0, value=999)
    ns = run_batch(tmp_path, [line_q, line_p], ["P", "Q"])
    target = target_for(
        ns, issue_code="overflow", quality_code="overflow", point_key="P"
    )
    result = find(ns, [target])
    assert result.minimality["winning_original_lines"] == [2]
    assert result.target_present[0]["point_key"] == "P"

    # The same-named issue on Q alone must not satisfy the P target.
    lone = tmp_path / "q_only.jsonl"
    lone.write_text(line_q)
    lone_db = tmp_path / "q_only.db"
    run_capture(replay_path=lone, contract_path=ns.contract,
                db_path=lone_db, reports_dir=None)
    store = Store(lone_db, read_only=True)
    try:
        rows = store.conn.execute(
            "SELECT device_id, point_key, issue_code FROM issues "
            "WHERE line_no > 0"
        ).fetchall()
    finally:
        store.close()
    assert [(r["device_id"], r["point_key"], r["issue_code"]) for r in rows] == [
        ("dev1", "Q", "overflow")
    ]


def test_two_targets_keep_both_identities(tmp_path):
    bad_crc = _jsonl(
        T2, build_request(1, 3, 1, 1).hex(),
        corrupt_crc(bytes.fromhex(
            good_line(T2, 1).split('"response_hex":"', 1)[1].split('"', 1)[0]
        )).hex(),
    )
    exception = _jsonl(
        T4, build_request(1, 3, 2, 1).hex(),
        build_exception_response(1, 3, 2).hex(),
    )
    ns = run_batch(
        tmp_path,
        [good_line(T1, 0), bad_crc, good_line(T3, 0), exception, good_line(T5, 0)],
        ["P", "Q", "R"],
    )
    targets = [
        target_for(ns, issue_code="bad_crc_response", point_key="Q"),
        target_for(ns, issue_code="exception_response", point_key="R"),
    ]
    result = find(ns, targets)
    assert result.sub_line_count == 2
    assert result.minimality["winning_original_lines"] == [2, 4]
    assert result.line_mapping == [
        {"sub_line_no": 1, "original_line_no": 2},
        {"sub_line_no": 2, "original_line_no": 4},
    ]
    identities = {
        (row["point_key"], row["issue_code"]) for row in result.target_present
    }
    assert identities == {
        ("Q", "bad_crc_response"), ("R", "exception_response")
    }


def test_tie_broken_by_original_line_number_sequence(tmp_path):
    # Three identical P@t1 frames: line 1 good, lines 2 and 3 both duplicate
    # with the *same* issue identity tuple.
    lines = [good_line(T1, 0), good_line(T1, 0), good_line(T1, 0)]
    ns = run_batch(tmp_path, lines, ["P", "Q"])
    target = target_for(ns, issue_code="duplicate", point_key="P")

    result = find(ns, [target])
    # Size-2 subsets {1,2} and {2,3} both reproduce the identity; lexicographic
    # ordering of original line-number sequences picks [1, 2].
    assert result.sub_line_count == 2
    assert result.minimality["winning_original_lines"] == [1, 2]

    # Prove the tie was real: {2, 3} independently reproduces the same target.
    alt_replay = tmp_path / "alt.jsonl"
    alt_replay.write_text(lines[1] + lines[2], encoding="utf-8")
    alt_db = tmp_path / "alt.db"
    run_capture(replay_path=alt_replay, contract_path=ns.contract,
                db_path=alt_db, reports_dir=None)
    store = Store(alt_db, read_only=True)
    try:
        row = store.conn.execute(
            "SELECT * FROM issues WHERE issue_code='duplicate' "
            "AND device_id=? AND point_key=? AND ts=?",
            (target.device_id, target.point_key, target.ts),
        ).fetchone()
    finally:
        store.close()
    assert row is not None


def test_input_over_limit_is_refused_without_greedy_answer(tmp_path):
    keys = ["P", "Q"]
    lines = [good_line(f"2025-01-01T00:00:{i:02d}Z", 0) for i in range(12)]
    bad = _jsonl(
        "2025-01-01T00:00:59Z", build_request(1, 3, 1, 1).hex(),
        corrupt_crc(bytes.fromhex(
            good_line(T2, 1).split('"response_hex":"', 1)[1].split('"', 1)[0]
        )).hex(),
    )
    ns = run_batch(tmp_path, lines + [bad], keys)
    target = target_for(ns, issue_code="bad_crc_response")

    with pytest.raises(MinReplayLimitError) as excinfo:
        find(ns, [target])
    assert excinfo.value.code == "input_too_large"
    assert "12" in excinfo.value.detail

    # A smaller explicit limit also refuses; raising it permits exact search.
    small_ns = run_batch(
        tmp_path, [good_line(T1, 0), bad, good_line(T3, 0)],
        keys, name="small.jsonl",
    )
    small_target = target_for(small_ns, issue_code="bad_crc_response")
    with pytest.raises(MinReplayLimitError):
        find(small_ns, [small_target], max_input_lines=2)
    ok = find(small_ns, [small_target], max_input_lines=3)
    assert ok.status == "optimal"
    assert ok.minimality["winning_original_lines"] == [2]


def test_demo_replay_over_default_limit_is_refused(demo_run):
    ns = SimpleNamespace(
        db=demo_run.paths.db, batch_id=demo_run.result.batch_id
    )
    target = target_for(ns, issue_code="duplicate", point_key="line_a_speed")
    with pytest.raises(MinReplayLimitError):
        find(ns, [target])


def test_original_batch_replay_and_contract_are_never_modified(tmp_path):
    ns = run_batch(
        tmp_path,
        [good_line(T1, 0), good_line(T1, 0), good_line(T3, 1)],
        ["P", "Q", "R"],
    )
    before = (sha256_of(ns.replay), sha256_of(ns.contract), sha256_of(ns.db))
    target = target_for(ns, issue_code="duplicate", point_key="P")

    result = find(ns, [target])
    out_dir = tmp_path / "bundle"
    bundle = write_min_replay_bundle(result, out_dir)
    after = (sha256_of(ns.replay), sha256_of(ns.contract), sha256_of(ns.db))
    assert before == after

    # The sub-replay is made of complete physical lines in original order,
    # with every byte (frame, timestamp, source) untouched.
    original_lines = ns.replay.read_bytes().splitlines(keepends=True)
    sub_bytes = (out_dir / "sub_replay.jsonl").read_bytes()
    expected = b"".join(
        original_lines[index - 1]
        for index in result.minimality["winning_original_lines"]
    )
    assert sub_bytes == expected
    assert bundle["sub_replay_sha256"] == hashlib.sha256(sub_bytes).hexdigest()

    proof = json.loads((out_dir / "proof.json").read_text(encoding="utf-8"))
    assert proof["original_replay"]["sha256"] == before[0]
    assert proof["contract_sha256"] == before[1]
    assert proof["sub_replay"]["sha256"] == bundle["sub_replay_sha256"]
    assert proof["minimality"]["search"] == "exact_exhaustive"


def test_rerun_produces_byte_identical_outputs(tmp_path):
    ns = run_batch(
        tmp_path,
        [good_line(T1, 0), good_line(T1, 0), good_line(T3, 1)],
        ["P", "Q", "R"],
    )
    target = target_for(ns, issue_code="duplicate", point_key="P")
    first = write_min_replay_bundle(find(ns, [target]), tmp_path / "run1")
    second = write_min_replay_bundle(find(ns, [target]), tmp_path / "run2")
    for name in ("sub_replay.jsonl", "proof.json"):
        assert (tmp_path / "run1" / name).read_bytes() == (
            tmp_path / "run2" / name
        ).read_bytes()
    assert first["proof_sha256"] == second["proof_sha256"]
    assert first["sub_replay_sha256"] == second["sub_replay_sha256"]


def test_validation_errors(tmp_path):
    ns = run_batch(
        tmp_path,
        [good_line(T1, 0), good_line(T1, 0)],
        ["P", "Q"],
    )
    target = target_for(ns, issue_code="duplicate", point_key="P")

    with pytest.raises(MinReplayError) as excinfo:
        find_min_replay(
            batch_db_path=ns.db, batch_id="does_not_exist", targets=(target,)
        )
    assert excinfo.value.code == "batch_not_found"

    ghost = TargetSpec(
        device_id="dev1", point_key="P", issue_code="overflow",
        quality_code="overflow", ts=T1,
    )
    with pytest.raises(MinReplayError) as excinfo:
        find(ns, [ghost])
    assert excinfo.value.code == "target_not_in_batch"

    with pytest.raises(MinReplayError) as excinfo:
        find(ns, [target], mode="bogus")
    assert excinfo.value.code == "invalid_mode"


def test_device_mode_requires_single_device_targets(tmp_path):
    contract = tmp_path / "two.yaml"
    contract.write_text(
        "version: 1\ndevices:\n"
        "  - id: dev1\n    label: d1\n    points:\n"
        "      - key: P\n        slave_id: 1\n        function: 3\n        start: 0\n"
        "        count: 1\n        data_type: uint16\n        byte_order: AB\n"
        "        scale: 1\n        offset: 0\n        unit: ''\n        range: [0, 100]\n"
        "      - key: P2\n        slave_id: 1\n        function: 3\n        start: 1\n"
        "        count: 1\n        data_type: uint16\n        byte_order: AB\n"
        "        scale: 1\n        offset: 0\n        unit: ''\n        range: [0, 100]\n"
        "  - id: dev2\n    label: d2\n    points:\n"
        "      - key: Q\n        slave_id: 2\n        function: 3\n        start: 0\n"
        "        count: 1\n        data_type: uint16\n        byte_order: AB\n"
        "        scale: 1\n        offset: 0\n        unit: ''\n        range: [0, 100]\n"
        "      - key: Q2\n        slave_id: 2\n        function: 3\n        start: 1\n"
        "        count: 1\n        data_type: uint16\n        byte_order: AB\n"
        "        scale: 1\n        offset: 0\n        unit: ''\n        range: [0, 100]\n",
        encoding="utf-8",
    )
    replay = tmp_path / "two.jsonl"
    replay.write_text(
        good_line(T1, 0, slave=1) + good_line(T2, 0, slave=2),
        encoding="utf-8",
    )
    db = tmp_path / "two.db"
    run_capture(replay_path=replay, contract_path=contract,
                db_path=db, reports_dir=None)
    ns = SimpleNamespace(db=db, batch_id=_only_batch(db), replay=replay,
                         contract=contract)
    # P2 and Q2 never appear: each device gets its own synthetic missing issue.
    t1 = target_for(ns, device_id="dev1", point_key="P2", line_no=0)
    t2 = target_for(ns, device_id="dev2", point_key="Q2", line_no=0)
    with pytest.raises(MinReplayError) as excinfo:
        find(ns, [t1, t2], mode=DEVICE_MODE)
    assert excinfo.value.code == "device_mode_requires_single_device"


def _only_batch(db_path):
    store = Store(db_path, read_only=True)
    try:
        return store.conn.execute("SELECT id FROM batches LIMIT 1").fetchone()[0]
    finally:
        store.close()


def test_cli_parser_registers_min_replay():
    from pointlog.cli import build_parser
    from pointlog.min_replay import DEVICE_MODE, TARGET_MODE

    parser = build_parser()
    args = parser.parse_args([
        "min-replay", "b_1234",
        "--target", "dev1", "P",
        "--target", "dev1", "Q",
        "--issue-code", "bad_crc_response",
        "--quality-code", "bad_crc_response",
        "--ts", "2025-01-01T00:00:00Z",
        "--mode", DEVICE_MODE,
        "--max-lines", "10",
        "--output", "/tmp/diag",
    ])
    assert args.command == "min-replay"
    assert args.batch_id == "b_1234"
    assert args.target == [["dev1", "P"], ["dev1", "Q"]]
    assert args.mode == DEVICE_MODE
    assert args.max_lines == 10
    assert args.output == "/tmp/diag"

    default_args = parser.parse_args([
        "min-replay", "b_1234", "--target", "dev1", "P",
        "--issue-code", "duplicate", "--quality-code", "duplicate",
        "--ts", "2025-01-01T00:00:00Z", "--output", "/tmp/diag2",
    ])
    assert default_args.mode == TARGET_MODE
    assert default_args.max_lines == 12
