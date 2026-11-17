"""Offline sustained-drift review against a frozen robust baseline."""

from __future__ import annotations

import json

import pytest

from pointlog.drift import (
    DriftError,
    DriftRejected,
    build_baseline,
    detect_drift,
    get_baseline,
    list_baselines,
    parse_timestamp,
    save_baseline,
    verify_baseline,
)
from pointlog.report import write_reports
from pointlog.store import Store

from drift_support import (
    CONTRACT_PATH,
    POINT_KEY,
    capture,
    event_signature,
    load_demo_contract,
    make_record,
    point_result,
    stamp,
    training_records,
    write_replay,
)


@pytest.fixture
def lab(tmp_path):
    """A completed training batch plus helpers to mint detection batches."""

    class Lab:
        def __init__(self) -> None:
            self.root = tmp_path
            self.db = tmp_path / "pointlog.db"
            self.contract = load_demo_contract()
            self.train_replay = write_replay(
                tmp_path / "train.jsonl", training_records(self.contract)
            )
            self.train = capture(self.db, self.train_replay)
            self._counter = 0

        def detection(self, records, name=None):
            self._counter += 1
            path = write_replay(
                tmp_path / (name or f"detect_{self._counter}.jsonl"), records
            )
            return capture(self.db, path)

        def baseline(self, sampling="global", **kwargs):
            store = Store(self.db)
            try:
                baseline = build_baseline(
                    store,
                    training_batch_id=self.train.batch_id,
                    contract=self.contract,
                    sampling=sampling,
                    **kwargs,
                )
                save_baseline(store, baseline)
                return baseline
            finally:
                store.close()

        def detect(self, baseline, batch, **kwargs):
            store = Store(self.db)
            try:
                return detect_drift(
                    store,
                    baseline=baseline,
                    detection_batch_id=batch.batch_id,
                    contract=self.contract,
                    **kwargs,
                )
            finally:
                store.close()

    return Lab()


# ---------------------------------------------------------------------------
# The two sampling semantics
# ---------------------------------------------------------------------------


def test_global_reference_reports_sustained_upward_shift(lab):
    baseline = lab.baseline("global")
    point = next(p for p in baseline["points"] if p["point_key"] == POINT_KEY)
    assert point["status"] == "ready"
    assert point["median"] == 20.0
    assert point["mad"] == 1.0

    batch = lab.detection([
        make_record(lab.contract, 30, stamp(day=2, hour=12, minute=minute))
        for minute in (0, 10, 20)
    ])
    report = lab.detect(baseline, batch)
    result = point_result(report)
    assert result["status"] == "drift"
    assert result["layers_used"] == ["global"]
    assert event_signature(report) == [(
        "up", stamp(day=2, hour=12, minute=0),
        stamp(day=2, hour=12, minute=20), 10.0, "global",
    )]
    # Magnitude is the median-relative deviation, not an absolute fault claim.
    assert result["drifts"][0]["baseline"]["median"] == 20.0


def test_hourly_reference_sees_noon_as_normal(lab):
    baseline = lab.baseline("hourly")
    point = next(p for p in baseline["points"] if p["point_key"] == POINT_KEY)
    assert point["status"] == "ready"
    noon = point["hours"]["12"]
    assert noon["status"] == "ready"
    assert noon["median"] == 30.0
    assert noon["mad"] == 1.0
    # Every other hour has its own independent reference around 20.
    assert point["hours"]["13"]["median"] == 20.0

    batch = lab.detection([
        make_record(lab.contract, 30, stamp(day=2, hour=12, minute=minute))
        for minute in (0, 10, 20)
    ])
    report = lab.detect(baseline, batch)
    result = point_result(report)
    assert result["status"] == "stable"
    assert result["drifts"] == []
    assert result["layers_used"] == ["hour:12"]
    assert report["summary"]["drift_points"] == 0


def test_same_frozen_training_and_values_yield_opposite_conclusions(lab):
    global_baseline = lab.baseline("global")
    hourly_baseline = lab.baseline("hourly")
    records = [
        make_record(lab.contract, 30, stamp(day=2, hour=12, minute=minute))
        for minute in (0, 10, 20)
    ]
    batch = lab.detection(records)
    global_report = lab.detect(global_baseline, batch)
    hourly_report = lab.detect(hourly_baseline, batch)

    assert point_result(global_report)["status"] == "drift"
    assert point_result(hourly_report)["status"] == "stable"
    # Identical frozen training source and identical detection values.
    assert (
        global_report["baseline"]["training_digest"]
        == hourly_report["baseline"]["training_digest"]
    )
    assert (
        global_report["detection_batch"]["id"]
        == hourly_report["detection_batch"]["id"]
    )
    # The frozen reference is never updated from the detection batch mean.
    frozen_global = next(
        p for p in global_baseline["points"] if p["point_key"] == POINT_KEY
    )
    frozen_hourly = next(
        p for p in hourly_baseline["points"] if p["point_key"] == POINT_KEY
    )
    assert frozen_global["median"] == 20.0
    assert frozen_hourly["hours"]["12"]["median"] == 30.0


# ---------------------------------------------------------------------------
# Isolated spikes vs sustained runs
# ---------------------------------------------------------------------------


def test_isolated_spike_does_not_trigger(lab):
    baseline = lab.baseline("global")
    batch = lab.detection([
        make_record(lab.contract, 20, stamp(day=2, hour=1, minute=0)),
        make_record(lab.contract, 20, stamp(day=2, hour=1, minute=10)),
        make_record(lab.contract, 20, stamp(day=2, hour=1, minute=20)),
        make_record(lab.contract, 40, stamp(day=2, hour=1, minute=30)),  # spike
        make_record(lab.contract, 20, stamp(day=2, hour=1, minute=40)),
        make_record(lab.contract, 20, stamp(day=2, hour=1, minute=50)),
    ])
    report = lab.detect(baseline, batch)
    assert point_result(report)["status"] == "stable"
    assert point_result(report)["drifts"] == []


def test_short_run_of_two_is_not_enough(lab):
    baseline = lab.baseline("global")
    batch = lab.detection([
        make_record(lab.contract, 40, stamp(day=2, hour=1, minute=0)),
        make_record(lab.contract, 40, stamp(day=2, hour=1, minute=10)),
    ])
    report = lab.detect(baseline, batch)
    assert point_result(report)["drifts"] == []


def test_consecutive_run_triggers_and_reports_start_direction_magnitude(lab):
    baseline = lab.baseline("global")
    # 3 x MAD = 3; -4 deviates beyond that band.  The reported robust scale
    # stays the sigma-equivalent 1.4826 x MAD without changing the multiplier.
    batch = lab.detection([
        make_record(lab.contract, 20, stamp(day=2, hour=1, minute=0)),
        make_record(lab.contract, 16, stamp(day=2, hour=1, minute=10)),
        make_record(lab.contract, 16, stamp(day=2, hour=1, minute=20)),
        make_record(lab.contract, 16, stamp(day=2, hour=1, minute=30)),
        make_record(lab.contract, 20, stamp(day=2, hour=1, minute=40)),
    ])
    report = lab.detect(baseline, batch)
    point = point_result(report)
    assert point["status"] == "drift"
    event = point["drifts"][0]
    assert event["direction"] == "down"
    assert event["start"]["ts"] == stamp(day=2, hour=1, minute=10)
    assert event["detected_at"]["ts"] == stamp(day=2, hour=1, minute=30)
    assert event["magnitude"] == 4.0
    assert event["line_numbers"] == [2, 3, 4]
    # Baseline summary accompanies the event for auditability.
    assert event["baseline"]["median"] == 20.0
    assert event["baseline"]["mad"] == 1.0
    assert event["baseline"]["robust_scale"] == pytest.approx(1.4826)


def test_consecutive_counter_resets_on_normative_sample(lab):
    baseline = lab.baseline("global")
    batch = lab.detection([
        make_record(lab.contract, 40, stamp(day=2, hour=1, minute=0)),
        make_record(lab.contract, 40, stamp(day=2, hour=1, minute=10)),
        make_record(lab.contract, 20, stamp(day=2, hour=1, minute=20)),
        make_record(lab.contract, 40, stamp(day=2, hour=1, minute=30)),
    ])
    report = lab.detect(baseline, batch)
    assert point_result(report)["drifts"] == []


# ---------------------------------------------------------------------------
# Insufficient evidence
# ---------------------------------------------------------------------------


def test_mad_zero_with_constant_training_still_judges_exact_values(lab):
    # Constant reference: MAD == 0 and the layer is genuinely constant.
    replay = write_replay(
        lab.root / "constant_train.jsonl",
        [
            make_record(lab.contract, 20, stamp(day=1, hour=0, minute=index * 10))
            for index in range(5)
        ],
    )
    batch = capture(lab.db, replay)
    store = Store(lab.db)
    try:
        baseline = build_baseline(
            store, training_batch_id=batch.batch_id, contract=lab.contract
        )
    finally:
        store.close()
    point = next(p for p in baseline["points"] if p["point_key"] == POINT_KEY)
    assert point["status"] == "ready"
    assert point["mad"] == 0.0

    shift = lab.detection([
        make_record(lab.contract, 21, stamp(day=2, hour=0, minute=index * 10))
        for index in range(3)
    ])
    report = lab.detect(baseline, shift)
    assert point_result(report)["status"] == "drift"
    assert event_signature(report)[0][0] == "up"


def test_mad_zero_with_variable_training_is_insufficient(lab, tmp_path):
    # A different point whose training values vary yet still yield MAD == 0.
    # Two values with one at the median and the outlier far away: median=20,
    # absolute deviations = {0, 0, 0, 0, 30} whose median is 0.
    values = [20, 20, 20, 20, 50]
    replay = write_replay(
        tmp_path / "zero_mad.jsonl",
        [
            make_record(lab.contract, value, stamp(day=1, hour=0, minute=index * 10))
            for index, value in enumerate(values)
        ],
    )
    batch = capture(lab.db, replay)
    store = Store(lab.db)
    try:
        baseline = build_baseline(
            store, training_batch_id=batch.batch_id, contract=lab.contract
        )
    finally:
        store.close()
    point = next(
        p for p in baseline["points"]
        if p["point_key"] == POINT_KEY
    )
    assert point["mad"] == 0.0
    assert point["status"] == "insufficient"
    assert any(item["code"] == "mad_zero_variable" for item in point["evidence"])

    detection = lab.detection([
        make_record(lab.contract, 50, stamp(day=2, hour=0, minute=index * 10))
        for index in range(3)
    ])
    report = lab.detect(baseline, detection)
    result = point_result(report)
    assert result["status"] == "insufficient"
    assert result["drifts"] == []
    assert any(
        item["code"] == "mad_zero_variable" for item in result["evidence"]
    )


def test_training_with_too_few_samples_is_insufficient(lab, tmp_path):
    replay = write_replay(
        tmp_path / "sparse.jsonl",
        [
            make_record(lab.contract, 20 + index, stamp(day=1, hour=0, minute=index))
            for index in range(3)
        ],
    )
    batch = capture(lab.db, replay)
    store = Store(lab.db)
    try:
        baseline = build_baseline(
            store, training_batch_id=batch.batch_id, contract=lab.contract,
            min_samples=5,
        )
    finally:
        store.close()
    point = next(p for p in baseline["points"] if p["point_key"] == POINT_KEY)
    assert point["status"] == "insufficient"
    assert any(item["code"] == "insufficient_samples" for item in point["evidence"])

    detection = lab.detection([
        make_record(lab.contract, 20, stamp(day=2, hour=0, minute=index * 10))
        for index in range(3)
    ])
    report = lab.detect(baseline, detection)
    assert point_result(report)["status"] == "insufficient"
    assert point_result(report)["drifts"] == []


def test_long_training_gap_is_insufficient(lab, tmp_path):
    records = [
        make_record(lab.contract, 20 + (index % 3), stamp(day=1, hour=0, minute=index))
        for index in range(3)
    ]
    records += [
        make_record(lab.contract, 20 + (index % 3), stamp(day=3, hour=0, minute=index))
        for index in range(3)
    ]
    replay = write_replay(tmp_path / "gapped.jsonl", records)
    batch = capture(lab.db, replay)
    store = Store(lab.db)
    try:
        baseline = build_baseline(
            store, training_batch_id=batch.batch_id, contract=lab.contract,
            max_gap_seconds=3600,
        )
    finally:
        store.close()
    point = next(p for p in baseline["points"] if p["point_key"] == POINT_KEY)
    assert point["status"] == "insufficient"
    assert any(item["code"] == "training_gap" for item in point["evidence"])


def test_detection_gap_is_insufficient_and_breaks_run(lab):
    baseline = lab.baseline("global")
    batch = lab.detection([
        make_record(lab.contract, 40, stamp(day=2, hour=0, minute=0)),
        make_record(lab.contract, 40, stamp(day=2, hour=0, minute=10)),
        make_record(lab.contract, 40, stamp(day=3, hour=0, minute=0)),  # 24h gap
    ])
    report = lab.detect(baseline, batch)
    result = point_result(report)
    assert result["status"] == "insufficient"
    assert result["drifts"] == []
    assert result["insufficient"] is True
    assert any(item["code"] == "detection_gap" for item in result["evidence"])


def test_unusable_samples_never_count_as_zero_or_normal(lab):
    baseline = lab.baseline("global")
    batch = lab.detection([
        make_record(lab.contract, 40, stamp(day=2, hour=0, minute=0)),
        make_record(
            lab.contract, 40, stamp(day=2, hour=0, minute=10),
            corrupt_response_crc=True,
        ),
        make_record(lab.contract, 40, stamp(day=2, hour=0, minute=20)),
        make_record(lab.contract, 40, stamp(day=2, hour=0, minute=30)),
    ])
    report = lab.detect(baseline, batch)
    result = point_result(report)
    # Bad CRC breaks the contiguous run; the two frames after it are only two.
    assert result["drifts"] == []
    assert result["invalid_sample_count"] == 1
    assert any(
        item["code"] == "frame_bad_crc_response" for item in result["evidence"]
    )


def test_missing_response_frame_breaks_run(lab):
    baseline = lab.baseline("global")
    batch = lab.detection([
        make_record(lab.contract, 40, stamp(day=2, hour=0, minute=0)),
        make_record(lab.contract, 40, stamp(day=2, hour=0, minute=10)),
        make_record(
            lab.contract, 40, stamp(day=2, hour=0, minute=20), empty_response=True
        ),
        make_record(lab.contract, 40, stamp(day=2, hour=0, minute=30)),
        make_record(lab.contract, 40, stamp(day=2, hour=0, minute=40)),
    ])
    report = lab.detect(baseline, batch)
    assert point_result(report)["drifts"] == []
    assert any(
        item["code"] == "frame_missing_response"
        for item in point_result(report)["evidence"]
    )


# ---------------------------------------------------------------------------
# Hourly layering
# ---------------------------------------------------------------------------


def test_missing_hour_layer_is_insufficient_not_global_fallback(lab):
    baseline = lab.baseline("hourly")
    batch = lab.detection([
        make_record(lab.contract, 30, stamp(day=2, hour=3, minute=index * 10))
        for index in range(3)
    ])
    # Remove the 03 hour from the frozen hourly baseline to simulate an
    # hour that the training set never covered.
    trimmed = json.loads(json.dumps(baseline))
    point = next(p for p in trimmed["points"] if p["point_key"] == POINT_KEY)
    point["hours"].pop("03")

    report = lab.detect(trimmed, batch)
    result = point_result(report)
    assert result["status"] == "insufficient"
    assert result["drifts"] == []
    assert any(item["code"] == "hour_unavailable" for item in result["evidence"])
    # Crucially it did not silently fall back to the global monthly reference.
    assert result["layers_used"] == []


def test_hour_with_too_few_training_samples_is_not_ready(lab):
    baseline = lab.baseline("hourly", min_samples=5)
    point = next(p for p in baseline["points"] if p["point_key"] == POINT_KEY)
    for hour in ("00", "06", "12", "23"):
        assert point["hours"][hour]["status"] == "ready"
    # Shrink one hour's sample count in a copy and confirm the layer degrades.
    trimmed = json.loads(json.dumps(baseline))
    target = next(p for p in trimmed["points"] if p["point_key"] == POINT_KEY)
    target["hours"]["12"]["sample_count"] = 4
    target["hours"]["12"]["status"] = "insufficient"
    target["hours"]["12"]["reason"] = "insufficient_samples"
    batch = lab.detection([
        make_record(lab.contract, 30, stamp(day=2, hour=12, minute=index * 10))
        for index in range(3)
    ])
    report = lab.detect(trimmed, batch)
    result = point_result(report)
    assert result["status"] == "insufficient"
    assert any(
        item["code"] == "insufficient_samples" for item in result["evidence"]
    )


def test_hourly_run_may_not_span_two_reference_layers(lab):
    baseline = lab.baseline("hourly")
    # The hour-13 layer is around 20, so 30 is a two-sample run at 13:50 and
    # 13:59.  The next two samples belong to the separate hour-14 reference;
    # the run must reset at the layer boundary and never reach three.
    batch = lab.detection([
        make_record(lab.contract, 30, stamp(day=2, hour=13, minute=50)),
        make_record(lab.contract, 30, stamp(day=2, hour=13, minute=59)),
        make_record(lab.contract, 30, stamp(day=2, hour=14, minute=0)),
        make_record(lab.contract, 30, stamp(day=2, hour=14, minute=10)),
    ])
    report = lab.detect(baseline, batch)
    result = point_result(report)
    assert result["drifts"] == []
    assert result["layers_used"] == ["hour:13", "hour:14"]


def test_hourly_layers_are_independent_per_hour(lab):
    baseline = lab.baseline("hourly")
    point = next(p for p in baseline["points"] if p["point_key"] == POINT_KEY)
    assert point["hours"]["12"]["median"] == 30.0
    assert point["hours"]["00"]["median"] == 20.0
    batch = lab.detection([
        make_record(lab.contract, 30, stamp(day=2, hour=0, minute=index * 10))
        for index in range(3)
    ])
    report = lab.detect(baseline, batch)
    result = point_result(report)
    assert result["status"] == "drift"
    assert result["layers_used"] == ["hour:00"]
    assert event_signature(report)[0][4] == "hour:00"


# ---------------------------------------------------------------------------
# Baseline freeze / contamination / resume stability
# ---------------------------------------------------------------------------


def test_baseline_requires_explicit_completed_training_batch(lab, tmp_path):
    store = Store(lab.db)
    try:
        with pytest.raises(DriftError) as excinfo:
            build_baseline(
                store, training_batch_id="b_missing", contract=lab.contract
            )
        assert excinfo.value.code == "training_batch_missing"
    finally:
        store.close()

    replay = write_replay(
        tmp_path / "incomplete.jsonl",
        [make_record(lab.contract, 20, stamp(day=1, hour=0, minute=index))
         for index in range(4)],
    )
    from pointlog.pipeline import IngestInterrupted

    with pytest.raises(IngestInterrupted):
        capture(lab.db, replay, stop_after=2)
    store = Store(lab.db)
    try:
        from pointlog.store import make_batch_id
        from pointlog.util import sha256_file

        sha = sha256_file(replay)
        contract_sha = sha256_file(CONTRACT_PATH)
        partial_id = make_batch_id(sha, contract_sha)
        with pytest.raises(DriftError) as excinfo:
            build_baseline(store, training_batch_id=partial_id, contract=lab.contract)
        assert excinfo.value.code == "training_batch_incomplete"
    finally:
        store.close()


def test_detection_batch_cannot_be_the_training_batch(lab):
    baseline = lab.baseline("global")
    store = Store(lab.db)
    try:
        with pytest.raises(DriftRejected) as excinfo:
            detect_drift(
                store, baseline=baseline,
                detection_batch_id=lab.train.batch_id, contract=lab.contract,
            )
        assert excinfo.value.code == "detection_is_training"
    finally:
        store.close()


def test_protocol_threshold_and_consecutive_validation(lab):
    baseline = lab.baseline("global")
    store = Store(lab.db)
    try:
        with pytest.raises(DriftError) as excinfo:
            detect_drift(
                store, baseline=baseline,
                detection_batch_id=lab.train.batch_id, contract=lab.contract,
                threshold=0,
            )
        assert excinfo.value.code == "invalid_threshold"
        with pytest.raises(DriftError) as excinfo:
            detect_drift(
                store, baseline=baseline,
                detection_batch_id=lab.train.batch_id, contract=lab.contract,
                consecutive=0,
            )
        assert excinfo.value.code == "invalid_consecutive"
    finally:
        store.close()


def test_threshold_and_consecutive_are_configurable(lab):
    baseline = lab.baseline("global")
    # 22 is 2.0 away from the median: inside 3 x MAD but beyond 1.5 x MAD.
    records = [
        make_record(lab.contract, 22, stamp(day=2, hour=1, minute=index * 10))
        for index in range(3)
    ]
    batch = lab.detection(records)
    assert point_result(lab.detect(baseline, batch))["drifts"] == []
    assert event_signature(
        lab.detect(baseline, batch, threshold=1.5)
    )[0][0] == "up"
    assert event_signature(
        lab.detect(baseline, batch, threshold=1.5, consecutive=2)
    )[0][2] == stamp(day=2, hour=1, minute=10)


def test_detection_batch_must_be_completed(lab):
    from pointlog.pipeline import IngestInterrupted

    baseline = lab.baseline("global")
    replay = write_replay(
        lab.root / "partial_detect.jsonl",
        [
            make_record(lab.contract, 40, stamp(day=2, hour=1, minute=index * 10))
            for index in range(4)
        ],
    )
    with pytest.raises(IngestInterrupted):
        capture(lab.db, replay, stop_after=2)
    from pointlog.store import make_batch_id
    from pointlog.util import sha256_file

    partial_id = make_batch_id(
        sha256_file(replay), sha256_file(CONTRACT_PATH)
    )
    store = Store(lab.db)
    try:
        with pytest.raises(DriftError) as excinfo:
            detect_drift(
                store, baseline=baseline,
                detection_batch_id=partial_id, contract=lab.contract,
            )
        assert excinfo.value.code == "detection_batch_incomplete"
    finally:
        store.close()


def test_training_contamination_after_freeze_is_rejected(lab):
    baseline = lab.baseline("global")
    store = Store(lab.db)
    try:
        # Tamper with a stored training reading: the frozen digest no longer
        # matches the source data, so reuse must be refused.
        store.conn.execute(
            "UPDATE readings SET engineering_value = engineering_value + 5 "
            "WHERE batch_id = ? AND point_key = ?",
            (lab.train.batch_id, POINT_KEY),
        )
        store.conn.commit()
        with pytest.raises(DriftRejected) as excinfo:
            verify_baseline(store, baseline, lab.contract)
        assert excinfo.value.code == "training_input_changed"
    finally:
        store.close()


def test_contract_unit_change_invalidates_baseline(lab, tmp_path):
    baseline = lab.baseline("global")
    original = CONTRACT_PATH.read_text(encoding="utf-8")
    changed = tmp_path / "changed_contract.yaml"
    changed.write_text(
        original.replace(
            "        unit: degC\n        range: [-40, 150]",
            "        unit: kelvin\n        range: [-40, 150]",
        ),
        encoding="utf-8",
    )
    from pointlog.contract import load_contract

    changed_contract = load_contract(changed)
    store = Store(lab.db)
    try:
        with pytest.raises(DriftRejected) as excinfo:
            verify_baseline(store, baseline, changed_contract)
        assert excinfo.value.code == "contract_changed"
    finally:
        store.close()


def test_contract_calibration_change_scale_is_rejected(lab, tmp_path):
    baseline = lab.baseline("global")
    original = CONTRACT_PATH.read_text(encoding="utf-8")
    changed = tmp_path / "rescaled_contract.yaml"
    changed.write_text(
        original.replace(
            "        scale: 0.1\n        offset: 0\n        unit: degC",
            "        scale: 0.2\n        offset: 0\n        unit: degC",
        ),
        encoding="utf-8",
    )
    from pointlog.contract import load_contract

    store = Store(lab.db)
    try:
        with pytest.raises(DriftRejected) as excinfo:
            verify_baseline(store, baseline, load_contract(changed))
        assert excinfo.value.code == "contract_changed"
    finally:
        store.close()


def test_baseline_survives_store_roundtrip_and_lists(lab):
    baseline = lab.baseline("hourly")
    store = Store(lab.db)
    try:
        loaded = get_baseline(store, baseline["id"])
        assert loaded == baseline
        rows = list_baselines(store)
        assert [row["id"] for row in rows] == [baseline["id"]]
        assert rows[0]["training_digest"] == baseline["training_digest"]
        assert rows[0]["sampling"] == "hourly"
    finally:
        store.close()


def test_repeat_and_resume_reports_are_byte_identical(lab, tmp_path):
    baseline = lab.baseline("global")
    batch = lab.detection([
        make_record(lab.contract, 40, stamp(day=2, hour=1, minute=index * 10))
        for index in range(4)
    ])
    first_report = lab.detect(baseline, batch)
    second_report = lab.detect(baseline, batch)
    assert first_report == second_report

    store = Store(lab.db)
    try:
        write_reports(
            store, batch.batch_id, lab.contract, tmp_path / "a", drift=first_report
        )
        write_reports(
            store, batch.batch_id, lab.contract, tmp_path / "b", drift=second_report
        )
    finally:
        store.close()
    for name in ("summary.json", "summary.md", "summary.html", "drift_review.csv"):
        assert (tmp_path / "a" / name).read_bytes() == (
            tmp_path / "b" / name
        ).read_bytes()


def test_baseline_construction_is_stable_across_rebuilds(lab):
    first = lab.baseline("global")
    second = lab.baseline("global")
    assert first == second
    assert first["id"] == second["id"]


def test_detection_does_not_mutate_readings_or_quality(lab):
    baseline = lab.baseline("global")
    batch = lab.detection([
        make_record(lab.contract, 40, stamp(day=2, hour=0, minute=index * 10))
        for index in range(3)
    ])
    store = Store(lab.db, read_only=True)
    try:
        before = [
            tuple(row)
            for row in store.conn.execute(
                "SELECT * FROM readings WHERE batch_id = ? ORDER BY id",
                (batch.batch_id,),
            )
        ]
        quality_before = store.conn.execute(
            "SELECT quality_code FROM frames WHERE batch_id = ? ORDER BY line_no",
            (batch.batch_id,),
        ).fetchall()
    finally:
        store.close()

    report = lab.detect(baseline, batch)
    assert point_result(report)["status"] == "drift"

    store = Store(lab.db, read_only=True)
    try:
        after = [
            tuple(row)
            for row in store.conn.execute(
                "SELECT * FROM readings WHERE batch_id = ? ORDER BY id",
                (batch.batch_id,),
            )
        ]
        quality_after = store.conn.execute(
            "SELECT quality_code FROM frames WHERE batch_id = ? ORDER BY line_no",
            (batch.batch_id,),
        ).fetchall()
    finally:
        store.close()
    assert after == before
    assert quality_after == quality_before


# ---------------------------------------------------------------------------
# Ordering and resume determinism
# ---------------------------------------------------------------------------


def test_out_of_order_input_is_ordered_by_timestamp(lab):
    baseline = lab.baseline("global")
    batch = lab.detection([
        make_record(lab.contract, 20, stamp(day=2, hour=1, minute=40)),
        make_record(lab.contract, 40, stamp(day=2, hour=1, minute=10)),
        make_record(lab.contract, 40, stamp(day=2, hour=1, minute=0)),
        make_record(lab.contract, 20, stamp(day=2, hour=1, minute=50)),
        make_record(lab.contract, 40, stamp(day=2, hour=1, minute=20)),
    ])
    report = lab.detect(baseline, batch)
    assert point_result(report)["status"] == "drift"
    assert event_signature(report) == [(
        "up", stamp(day=2, hour=1, minute=0),
        stamp(day=2, hour=1, minute=20), 20.0, "global",
    )]
    # The reported line numbers follow the on-disk source lines, not the
    # chronological position.
    assert point_result(report)["drifts"][0]["line_numbers"] == [3, 2, 5]


def test_resumed_detection_is_byte_identical(lab):
    from pointlog.pipeline import IngestInterrupted

    baseline = lab.baseline("global")
    detection_replay = write_replay(
        lab.root / "resume_detect.jsonl",
        [
            make_record(lab.contract, 40, stamp(day=2, hour=1, minute=index * 10))
            for index in range(6)
        ],
    )
    with pytest.raises(IngestInterrupted):
        capture(lab.db, detection_replay, stop_after=2)
    from pointlog.pipeline import run_capture

    result = run_capture(
        replay_path=detection_replay,
        contract_path=CONTRACT_PATH,
        db_path=lab.db,
        reports_dir=None,
        resume=True,
    )
    store = Store(lab.db)
    try:
        resumed_report = detect_drift(
            store, baseline=baseline, detection_batch_id=result.batch_id,
            contract=lab.contract,
        )
    finally:
        store.close()

    # A fresh database with the same detection values yields the same verdict.
    fresh = lab.root / "fresh"
    fresh.mkdir()
    fresh_db = fresh / "pointlog.db"
    train = capture(fresh_db, lab.train_replay)
    full = capture(fresh_db, detection_replay)
    store = Store(fresh_db)
    try:
        fresh_baseline = build_baseline(
            store, training_batch_id=train.batch_id, contract=lab.contract
        )
        full_report = detect_drift(
            store, baseline=fresh_baseline, detection_batch_id=full.batch_id,
            contract=lab.contract,
        )
    finally:
        store.close()

    assert resumed_report["summary"] == full_report["summary"]
    assert point_result(resumed_report)["drifts"] == point_result(
        full_report
    )["drifts"]


# ---------------------------------------------------------------------------
# Report integration
# ---------------------------------------------------------------------------


def test_report_shows_reference_layer_and_insufficient_evidence(lab, tmp_path):
    baseline = lab.baseline("hourly")
    batch = lab.detection([
        make_record(lab.contract, 30, stamp(day=2, hour=12, minute=index * 10))
        for index in range(3)
    ])
    report = lab.detect(baseline, batch)
    store = Store(lab.db)
    try:
        paths = write_reports(
            store, batch.batch_id, lab.contract, tmp_path / "reports",
            drift=report,
        )
    finally:
        store.close()
    assert "drift_csv" in paths

    summary = json.loads(
        (tmp_path / "reports" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["drift"]["sampling"] == "hourly"
    point = next(
        item for item in summary["points"] if item["point_key"] == POINT_KEY
    )
    assert point["drift_status"] == "stable"
    assert point["drift_reference_layers"] == ["hour:12"]

    markdown = (tmp_path / "reports" / "summary.md").read_text(encoding="utf-8")
    assert "## 稳健漂移复核" in markdown
    assert "UTC 小时分层" in markdown
    assert "不足以判断" in markdown
    assert "不是设备故障事实" in markdown

    html = (tmp_path / "reports" / "summary.html").read_text(encoding="utf-8")
    assert "稳健漂移复核" in html
    assert "参考层与置信不足" in html

    import csv

    with (tmp_path / "reports" / "drift_review.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0])[:4] == [
        "device_id", "point_key", "unit", "reference"
    ]
    assert any(row["status"] == "insufficient" for row in rows)


def test_global_report_surfaces_drift_event_in_all_formats(lab, tmp_path):
    baseline = lab.baseline("global")
    batch = lab.detection([
        make_record(lab.contract, 40, stamp(day=2, hour=1, minute=index * 10))
        for index in range(3)
    ])
    report = lab.detect(baseline, batch)
    store = Store(lab.db)
    try:
        write_reports(
            store, batch.batch_id, lab.contract, tmp_path / "reports",
            drift=report,
        )
    finally:
        store.close()
    markdown = (tmp_path / "reports" / "summary.md").read_text(encoding="utf-8")
    assert "| global | up |" in markdown


# ---------------------------------------------------------------------------
# Timestamp legality
# ---------------------------------------------------------------------------


def test_naive_timestamp_is_illegal():
    assert parse_timestamp("2025-04-01T00:00:00") is None
    assert parse_timestamp("not-a-timestamp") is None
    assert parse_timestamp("") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("2025-04-01T00:00:00Z") is not None
    assert parse_timestamp("2025-04-01T00:00:00+08:00") is not None
