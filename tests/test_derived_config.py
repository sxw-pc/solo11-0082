"""Static derived-contract validation: DAG, devices, dimensions, divisors."""

from __future__ import annotations

import pytest

from pointlog.derived import (
    DerivedConfigError,
    parse_derived_config,
    parse_expression,
    walk_refs,
)

from derived_helpers import BASE_CONTRACT, make_contract

CONTRACT = make_contract(BASE_CONTRACT)


def expect_code(doc: str, code: str):
    with pytest.raises(DerivedConfigError) as excinfo:
        parse_derived_config(doc, CONTRACT)
    assert excinfo.value.code == code, str(excinfo.value)
    return excinfo.value


POWER_INPUTS = """
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
      i: {point: curr, abs_error: 0.2, unit: A}
"""


def test_valid_power_contract_parses_and_fingerprints():
    doc = f"""
version: 1
error_semantics: guaranteed
derived_points:
  - device_id: dev1
    key: power_w
    expression: v * i
    unit: W
{POWER_INPUTS}"""
    config = parse_derived_config(doc, CONTRACT)
    assert config.default_semantics == "guaranteed"
    assert config.expression_version.startswith("ev_")
    point = config.by_key["power_w"]
    assert point.unit.label == "W"
    assert point.inputs[0].abs_error == 1
    # Same text yields the same expression version.
    again = parse_derived_config(doc, CONTRACT)
    assert again.expression_version == config.expression_version


def test_cycle_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: a
    expression: b
    unit: V
    inputs: {b: {point: b, abs_error: 0, unit: V}}
  - device_id: dev1
    key: b
    expression: a
    unit: V
    inputs: {a: {point: a, abs_error: 0, unit: V}}
""", "derived_cycle")


def test_self_cycle_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: a
    expression: x
    unit: V
    inputs: {x: {point: a, abs_error: 0, unit: V}}
""", "derived_cycle")


def test_cross_device_reference_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: x
    unit: V
    inputs: {x: {point: other_v, abs_error: 1, unit: V}}
""", "derived_cross_device")


def test_incompatible_addition_rejected():
    expect_code(f"""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: v + i
    unit: V
{POWER_INPUTS}""", "derived_dimension")


def test_compatible_addition_accepted():
    config = parse_derived_config("""
version: 1
derived_points:
  - device_id: dev1
    key: v_sum
    expression: a + b
    unit: V
    inputs:
      a: {point: volt, abs_error: 1, unit: V}
      b: {point: millivolt, abs_error: 100, unit: mV}
""", CONTRACT)
    assert config.by_key["v_sum"].unit.label == "V"


def test_output_unit_dimension_mismatch_rejected():
    expect_code(f"""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: v * i
    unit: V
{POWER_INPUTS}""", "derived_output_unit")


def test_affine_multiplication_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: t * i
    unit: W
    inputs:
      t: {point: temp, abs_error: 1, unit: degC}
      i: {point: curr, abs_error: 0.2, unit: A}
""", "derived_affine")


def test_affine_addition_rejected_but_delta_accepted():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: t1 + t2
    unit: degC
    inputs:
      t1: {point: temp, abs_error: 0.5, unit: degC}
      t2: {point: temp_b, abs_error: 0.5, unit: degC}
""", "derived_affine")
    config = parse_derived_config("""
version: 1
derived_points:
  - device_id: dev1
    key: t_delta
    expression: t1 - t2
    unit: degC
    inputs:
      t1: {point: temp, abs_error: 0.5, unit: degC}
      t2: {point: temp_b, abs_error: 0.5, unit: degC}
""", CONTRACT)
    # The delta output keeps the degC scale but drops the affine offset.
    from pointlog.codec import parse_unit

    assert config.by_key["t_delta"].unit.dimension == parse_unit("degC").dimension


def test_divisor_zero_crossing_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: v / x
    unit: A
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
      x: {point: rpos, abs_error: 2, unit: Ohm}
""", "derived_divide_by_zero")


def test_divisor_clear_of_zero_accepted():
    parse_derived_config("""
version: 1
derived_points:
  - device_id: dev1
    key: current_calc
    expression: v / x
    unit: A
    inputs:
      v: {point: volt, abs_error: 0, unit: V}
      x: {point: rpos, abs_error: 0, unit: Ohm}
""", CONTRACT)


def test_undeclared_input_reference_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: v * ghost
    unit: W
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
""", "derived_unknown_input")


def test_unknown_point_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: v
    unit: W
    inputs:
      v: {point: ghost, abs_error: 1, unit: W}
""", "derived_unknown_point")


def test_input_unit_dimension_mismatch_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: v
    unit: V
    inputs:
      v: {point: volt, abs_error: 1, unit: A}
""", "derived_unit_mismatch")


def test_unknown_device_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: nowhere
    key: p
    expression: v
    unit: V
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
""", "derived_unknown_device")


@pytest.mark.parametrize("semantics", ["guaranteed", "independent"])
def test_semantics_values_accepted(semantics):
    config = parse_derived_config(f"""
version: 1
error_semantics: {semantics}
derived_points:
  - device_id: dev1
    key: p
    expression: v
    unit: V
    inputs:
      v: {{point: volt, abs_error: 1, unit: V}}
""", CONTRACT)
    assert config.by_key["p"].error_semantics == semantics


def test_bad_semantics_rejected():
    expect_code("""
version: 1
error_semantics: wishful
derived_points:
  - device_id: dev1
    key: p
    expression: v
    unit: V
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
""", "derived_semantics")


def test_mixed_semantics_in_one_chain_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: power
    expression: v * i
    unit: W
    error_semantics: guaranteed
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
      i: {point: curr, abs_error: 0.2, unit: A}
  - device_id: dev1
    key: energy
    expression: p * t
    unit: J
    error_semantics: independent
    inputs:
      p: {point: power, abs_error: 0, unit: W}
      t: {point: dur, abs_error: 0.5, unit: s}
""", "derived_semantics_mix")


def test_disconnected_points_may_use_different_semantics():
    config = parse_derived_config("""
version: 1
derived_points:
  - device_id: dev1
    key: power_g
    expression: v * i
    unit: W
    error_semantics: guaranteed
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
      i: {point: curr, abs_error: 0.2, unit: A}
  - device_id: dev1
    key: duration_i
    expression: t
    unit: s
    error_semantics: independent
    inputs:
      t: {point: dur, abs_error: 0.5, unit: s}
""", CONTRACT)
    assert config.by_key["power_g"].error_semantics == "guaranteed"
    assert config.by_key["duration_i"].error_semantics == "independent"


def test_derived_key_colliding_with_raw_point_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: volt
    expression: v
    unit: V
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
""", "derived_duplicate_key")


def test_duplicate_derived_key_rejected():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: v
    unit: V
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
  - device_id: dev1
    key: p
    expression: v
    unit: V
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
""", "derived_duplicate_key")


@pytest.mark.parametrize("expression", ["v *", "(v + i", "v @ i", ""])
def test_bad_expressions_rejected(expression):
    expect_code(f"""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: "{expression}"
    unit: V
    inputs:
      v: {{point: volt, abs_error: 1, unit: V}}
""", "derived_expression")


@pytest.mark.parametrize("error", [-1, "nan"])
def test_bad_abs_error_rejected(error):
    expect_code(f"""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: v
    unit: V
    inputs:
      v: {{point: volt, abs_error: {error}, unit: V}}
""", "derived_input")


def test_threshold_validation():
    expect_code("""
version: 1
derived_points:
  - device_id: dev1
    key: p
    expression: v
    unit: V
    threshold: {direction: sideways, value: 1}
    inputs:
      v: {point: volt, abs_error: 1, unit: V}
""", "derived_threshold")


def test_multi_level_dag_resolves_upstream_dimensions():
    config = parse_derived_config("""
version: 1
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
""", CONTRACT)
    assert [p.point_key for p in config.points] == ["power", "energy"]


def test_expression_parser_collects_all_references():
    node = parse_expression("(v * i) + 2 / t")
    assert walk_refs(node) == {"v", "i", "t"}
    assert walk_refs(parse_expression("3.14")) == set()


def test_order_independent_dag_validation():
    # Downstream declared before upstream must still validate.
    config = parse_derived_config("""
version: 1
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
""", CONTRACT)
    assert config.by_key["energy"].expression_version == config.expression_version
