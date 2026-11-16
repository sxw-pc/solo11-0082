"""Runtime derivation: both error semantics, thresholds, chains and quality."""

from __future__ import annotations

import json
import math
from decimal import Decimal

import pytest

from pointlog.codec import parse_unit
from pointlog.derived import (
    DERIVED_GOOD,
    DERIVED_INDETERMINATE,
    GUARANTEED,
    INDEPENDENT,
    REASON_BAD_QUALITY,
    REASON_CONTRADICTION,
    REASON_CRC,
    REASON_MISSING,
    REASON_NON_FINITE,
    REASON_UPSTREAM,
    align_samples,
)

from derived_helpers import (
    LedgerHarness,
    POWER_DERIVED_GUARANTEED,
    POWER_DERIVED_INDEPENDENT,
    make_derived,
)

TS = "2025-03-01T00:00:00Z"


# --- Canonical 10 V * 2 A worked example ------------------------------------


def test_guaranteed_power_bounds_match_spec(tmp_path):
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V", line_no=1)
        harness.add_reading("curr", TS, 2.0, "A", line_no=2)
        config = make_derived(POWER_DERIVED_GUARANTEED)
        result = harness.materialize(config)
        assert result.good_rows == 1
        row = harness.derived_rows("power_w")[0]
        assert row["quality_code"] == DERIVED_GOOD
        assert row["engineering_value"] == pytest.approx(20.0)
        assert row["lower_bound"] == pytest.approx(16.2)
        assert row["upper_bound"] == pytest.approx(24.2)
        assert row["bound_sigma"] == ""
        assert row["error_semantics"] == GUARANTEED
        # The 23 W threshold can be crossed inside the guaranteed band.
        assert row["threshold_state"] == "possibly_above"
        assert json.loads(row["sources_json"]) == [
            {"point_key": "volt", "line_no": 1, "quality_code": "good"},
            {"point_key": "curr", "line_no": 2, "quality_code": "good"},
        ]
        assert json.loads(row["line_no_json"]) == [1, 2]


def test_independent_power_sigma_band_matches_spec(tmp_path):
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V", line_no=1)
        harness.add_reading("curr", TS, 2.0, "A", line_no=2)
        config = make_derived(POWER_DERIVED_INDEPENDENT)
        harness.materialize(config)
        row = harness.derived_rows("power_w")[0]
        assert row["engineering_value"] == pytest.approx(20.0)
        assert row["lower_bound"] == pytest.approx(17.171572875)
        assert row["upper_bound"] == pytest.approx(22.828427125)
        assert row["bound_sigma"] == "1sigma"
        assert row["error_semantics"] == INDEPENDENT
        # The 1-sigma band stays below 23 W: estimated within limit, which is
        # a different verdict from the guaranteed possibly_above verdict.
        assert row["threshold_state"] == "within"


def test_the_two_semantics_must_not_share_a_threshold_verdict(tmp_path):
    """The headline divergence: same 20 W center, different 23 W verdicts."""
    verdicts = {}
    for name, doc in (("guaranteed", POWER_DERIVED_GUARANTEED),
                      ("independent", POWER_DERIVED_INDEPENDENT)):
        store_harness = _fresh_harness(tmp_path, name)
        store_harness.add_reading("volt", TS, 10.0, "V", line_no=1)
        store_harness.add_reading("curr", TS, 2.0, "A", line_no=2)
        store_harness.materialize(make_derived(doc))
        verdicts[name] = store_harness.derived_rows("power_w")[0]["threshold_state"]
        store_harness.close()
    assert verdicts["guaranteed"] == "possibly_above"
    assert verdicts["independent"] == "within"
    assert verdicts["guaranteed"] != verdicts["independent"]


def _fresh_harness(tmp_path, name):
    sub = tmp_path / name
    sub.mkdir()
    return LedgerHarness(sub, batch_id=f"b_{name}")


# --- Threshold state machine -------------------------------------------------


def test_threshold_above_fully_crossed(tmp_path):
    doc = POWER_DERIVED_GUARANTEED.replace("value: 23", "value: 10")
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V")
        harness.add_reading("curr", TS, 2.0, "A")
        harness.materialize(make_derived(doc))
        assert harness.derived_rows("power_w")[0]["threshold_state"] == "above"


def test_threshold_below_direction(tmp_path):
    doc = """
version: 1
derived_points:
  - device_id: dev1
    key: low_power
    expression: v * i
    unit: W
    threshold: {direction: below, value: 30}
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
      i: {point: curr, abs_error: 0.2, unit: A}
"""
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V")
        harness.add_reading("curr", TS, 2.0, "A")
        harness.materialize(make_derived(doc))
        # Whole band [16.2, 24.2] is below 30.
        assert harness.derived_rows("low_power")[0]["threshold_state"] == "below"


# --- Prefix and affine handling ----------------------------------------------


def test_millivolt_input_converts_exactly(tmp_path):
    doc = """
version: 1
derived_points:
  - device_id: dev1
    key: power_w
    expression: v * i
    unit: W
    inputs:
      v: {point: millivolt, abs_error: 1000, unit: mV}
      i: {point: curr, abs_error: 0.2, unit: A}
"""
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("millivolt", TS, 10000.0, "mV")
        harness.add_reading("curr", TS, 2.0, "A")
        harness.materialize(make_derived(doc))
        row = harness.derived_rows("power_w")[0]
        assert row["engineering_value"] == pytest.approx(20.0)
        assert row["lower_bound"] == pytest.approx(16.2)
        assert row["upper_bound"] == pytest.approx(24.2)


def test_kilowatt_output_is_exact_decimal_conversion(tmp_path):
    doc = """
version: 1
derived_points:
  - device_id: dev1
    key: power_kw
    expression: v * i
    unit: kW
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
      i: {point: curr, abs_error: 0.2, unit: A}
"""
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V")
        harness.add_reading("curr", TS, 2.0, "A")
        harness.materialize(make_derived(doc))
        row = harness.derived_rows("power_kw")[0]
        assert row["engineering_value"] == pytest.approx(0.02)
        assert row["lower_bound"] == pytest.approx(0.0162)
        assert row["upper_bound"] == pytest.approx(0.0242)


def test_temperature_delta_is_allowed_and_offset_free(tmp_path):
    doc = """
version: 1
derived_points:
  - device_id: dev1
    key: t_delta
    expression: t1 - t2
    unit: degC
    inputs:
      t1: {point: temp, abs_error: 0.5, unit: degC}
      t2: {point: temp_b, abs_error: 0.5, unit: degC}
"""
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("temp", TS, 25.0, "degC")
        harness.add_reading("temp_b", TS, 20.0, "degC")
        harness.materialize(make_derived(doc))
        row = harness.derived_rows("t_delta")[0]
        assert row["engineering_value"] == pytest.approx(5.0)
        assert row["lower_bound"] == pytest.approx(4.0)
        assert row["upper_bound"] == pytest.approx(6.0)


# --- Indeterminacy reasons ---------------------------------------------------


def test_missing_source_is_indeterminate_with_reason(tmp_path):
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V")
        # curr absent at this timestamp
        harness.materialize(make_derived(POWER_DERIVED_GUARANTEED))
        row = harness.derived_rows("power_w")[0]
        assert row["quality_code"] == DERIVED_INDETERMINATE
        assert row["engineering_value"] is None
        assert row["lower_bound"] is None and row["upper_bound"] is None
        assert REASON_MISSING in row["reason_codes"].split(",")


def test_crc_failure_frame_makes_output_indeterminate(tmp_path):
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V")
        harness.add_frame("curr", TS, "bad_crc_response")
        harness.materialize(make_derived(POWER_DERIVED_GUARANTEED))
        row = harness.derived_rows("power_w")[0]
        assert row["quality_code"] == DERIVED_INDETERMINATE
        assert REASON_CRC in row["reason_codes"].split(",")
        assert row["engineering_value"] is None


def test_unacceptable_reading_quality_is_indeterminate(tmp_path):
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V")
        harness.add_reading("curr", TS, 2.0, "A", quality="duplicate")
        harness.materialize(make_derived(POWER_DERIVED_GUARANTEED))
        row = harness.derived_rows("power_w")[0]
        assert row["quality_code"] == DERIVED_INDETERMINATE
        assert REASON_BAD_QUALITY in row["reason_codes"].split(",")


def test_overflow_reading_still_derivable(tmp_path):
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V", quality="overflow")
        harness.add_reading("curr", TS, 2.0, "A")
        harness.materialize(make_derived(POWER_DERIVED_GUARANTEED))
        row = harness.derived_rows("power_w")[0]
        assert row["quality_code"] == DERIVED_GOOD


def test_align_samples_detects_contradiction():
    aligned = align_samples([
        {"ts": TS, "line_no": 1, "engineering_value": 10.0, "quality_code": "good"},
        {"ts": TS, "line_no": 2, "engineering_value": 12.0, "quality_code": "good"},
    ], "volt", parse_unit("V"), Decimal(1))
    assert aligned.failures[TS] == (REASON_CONTRADICTION,)
    assert TS not in aligned.samples


def test_align_samples_detects_non_finite():
    aligned = align_samples([
        {"ts": TS, "line_no": 1, "engineering_value": float("nan"),
         "quality_code": "good"},
    ], "volt", parse_unit("V"), Decimal(1))
    assert aligned.failures[TS] == (REASON_NON_FINITE,)


# --- Multi-level propagation -------------------------------------------------


CHAIN_DOC = """
version: 1
error_semantics: guaranteed
derived_points:
  - device_id: dev1
    key: power
    expression: v * i
    unit: W
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
      i: {point: curr, abs_error: 0.2, unit: A}
  - device_id: dev1
    key: energy
    expression: p * t
    unit: J
    inputs:
      p: {point: power, abs_error: 0, unit: W}
      t: {point: dur, abs_error: 0.5, unit: s}
"""


def test_multi_level_chain_propagates_bounds_and_provenance(tmp_path):
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V", line_no=1)
        harness.add_reading("curr", TS, 2.0, "A", line_no=2)
        harness.add_reading("dur", TS, 10.0, "s", line_no=3)
        harness.materialize(make_derived(CHAIN_DOC))
        power = harness.derived_rows("power")[0]
        energy = harness.derived_rows("energy")[0]
        assert power["engineering_value"] == pytest.approx(20.0)
        assert energy["engineering_value"] == pytest.approx(200.0)
        # Bounds propagated through the whole expression chain, not a final
        # percentage: [16.2, 24.2] W * [9.5, 10.5] s corner products.
        assert energy["lower_bound"] == pytest.approx(153.9)
        assert energy["upper_bound"] == pytest.approx(254.1)
        sources = {item["point_key"] for item in json.loads(energy["sources_json"])}
        assert sources == {"volt", "curr", "dur"}
        assert json.loads(energy["line_no_json"]) == [1, 2, 3]


def test_upstream_indeterminacy_propagates_downstream(tmp_path):
    with LedgerHarness(tmp_path) as harness:
        # curr missing -> power indeterminate -> energy indeterminate.
        harness.add_reading("volt", TS, 10.0, "V")
        harness.add_reading("dur", TS, 10.0, "s")
        harness.materialize(make_derived(CHAIN_DOC))
        power = harness.derived_rows("power")[0]
        energy = harness.derived_rows("energy")[0]
        assert power["quality_code"] == DERIVED_INDETERMINATE
        assert energy["quality_code"] == DERIVED_INDETERMINATE
        assert REASON_UPSTREAM in energy["reason_codes"].split(",")
        assert energy["engineering_value"] is None


def test_independent_chain_uses_rss_propagation(tmp_path):
    doc = CHAIN_DOC.replace("error_semantics: guaranteed",
                            "error_semantics: independent")
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V")
        harness.add_reading("curr", TS, 2.0, "A")
        harness.add_reading("dur", TS, 10.0, "s")
        harness.materialize(make_derived(doc))
        energy = harness.derived_rows("energy")[0]
        # sigma_power = sqrt((i*sigV)^2 + (V*sigI)^2) = sqrt(4+4) = sqrt(8)
        sigma_power = math.sqrt(8.0)
        # energy = power*t: sigma_E^2 = (t*sigma_P)^2 + (P*sigT)^2
        sigma_energy = math.sqrt((10 * sigma_power) ** 2 + (20 * 0.5) ** 2)
        assert energy["engineering_value"] == pytest.approx(200.0)
        assert energy["lower_bound"] == pytest.approx(200.0 - sigma_energy)
        assert energy["upper_bound"] == pytest.approx(200.0 + sigma_energy)
        assert energy["bound_sigma"] == "1sigma"


def test_runtime_divisor_zero_crossing_guard_remains_defensive(tmp_path):
    """Static validation makes this unreachable via config; the evaluator still
    refuses an interval divisor that crosses zero rather than emitting infinities."""
    from pointlog.derived import (
        DValue,
        REASON_DIVIDE_BY_ZERO,
        _guaranteed_evaluate,
        parse_expression,
    )
    from pointlog.codec import parse_unit

    node = parse_expression("v / x")
    volt = DValue(ok=True, value=Decimal(10), low=Decimal(10), high=Decimal(10),
                  sigma=Decimal(0), sources=(), unit=parse_unit("V"))
    crossing = DValue(ok=True, value=Decimal(0), low=Decimal(-1), high=Decimal(1),
                      sigma=Decimal(1), sources=(), unit=parse_unit("Ohm"))
    result = _guaranteed_evaluate(node, {"v": volt, "x": crossing})
    assert not result.ok
    assert REASON_DIVIDE_BY_ZERO in result.reasons
    assert result.value is None


def test_static_divisor_guard_is_complete(tmp_path):
    """Any divisor error wide enough to reach zero is rejected before ingest."""
    from pointlog.derived import DerivedConfigError

    doc = """
version: 1
derived_points:
  - device_id: dev1
    key: ratio
    expression: v / x
    unit: A
    inputs:
      v: {point: volt, abs_error: 0, unit: V}
      x: {point: rpos, abs_error: 5, unit: Ohm}
"""
    with pytest.raises(DerivedConfigError) as excinfo:
        make_derived(doc)
    assert excinfo.value.code == "derived_divide_by_zero"


def test_independent_division_band(tmp_path):
    doc = """
version: 1
error_semantics: independent
derived_points:
  - device_id: dev1
    key: current_calc
    expression: v / x
    unit: A
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
      x: {point: rpos, abs_error: 0.5, unit: Ohm}
"""
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V")
        harness.add_reading("rpos", TS, 5.0, "Ohm")
        harness.materialize(make_derived(doc))
        row = harness.derived_rows("current_calc")[0]
        assert row["quality_code"] == DERIVED_GOOD
        assert row["engineering_value"] == pytest.approx(2.0)
        sigma = math.sqrt((1 / 5) ** 2 + ((10 * 0.5) / 25) ** 2)
        assert row["lower_bound"] == pytest.approx(2.0 - sigma)
        assert row["upper_bound"] == pytest.approx(2.0 + sigma)


# --- Expression version and provenance --------------------------------------


def test_chain_materializes_when_downstream_is_declared_first(tmp_path):
    # Declaration order is child-before-parent; topological evaluation must
    # still produce both rows with correct propagated bounds.
    reverse_doc = """
version: 1
error_semantics: guaranteed
derived_points:
  - device_id: dev1
    key: energy
    expression: p * t
    unit: J
    inputs:
      p: {point: power, abs_error: 0, unit: W}
      t: {point: dur, abs_error: 0.5, unit: s}
  - device_id: dev1
    key: power
    expression: v * i
    unit: W
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
      i: {point: curr, abs_error: 0.2, unit: A}
"""
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V", line_no=1)
        harness.add_reading("curr", TS, 2.0, "A", line_no=2)
        harness.add_reading("dur", TS, 10.0, "s", line_no=3)
        harness.materialize(make_derived(reverse_doc))
        keys = {row["point_key"] for row in harness.derived_rows()}
        assert keys == {"power", "energy"}
        energy = harness.derived_rows("energy")[0]
        assert energy["quality_code"] == DERIVED_GOOD
        assert energy["engineering_value"] == pytest.approx(200.0)
        assert energy["lower_bound"] == pytest.approx(153.9)


def test_rows_record_expression_version(tmp_path):
    with LedgerHarness(tmp_path) as harness:
        harness.add_reading("volt", TS, 10.0, "V")
        harness.add_reading("curr", TS, 2.0, "A")
        config = make_derived(POWER_DERIVED_GUARANTEED)
        harness.materialize(config)
        row = harness.derived_rows("power_w")[0]
        assert row["expression_version"] == config.expression_version
        stored = harness.store.get_derived_contract(
            "d_" + config.fingerprint[:16])
        assert stored is not None
        assert stored["expression_version"] == config.expression_version
