"""Unit parsing, SI dimensions, exact prefix conversion and affine rules."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pointlog.codec import (
    UnitError,
    combine_dimensions,
    parse_unit,
    same_dimension,
)


def test_basic_electrical_dimensions():
    volt = parse_unit("V")
    amp = parse_unit("A")
    watt = parse_unit("W")
    assert combine_dimensions(volt, amp, True) == watt.dimension
    ohm = parse_unit("Ohm")
    assert combine_dimensions(volt, amp, False) == ohm.dimension


def test_prefix_factors_are_exact_decimal():
    assert parse_unit("kW").factor == Decimal("1000")
    assert parse_unit("W").factor == Decimal(1)
    assert parse_unit("mV").factor == Decimal("0.001")
    assert parse_unit("kWh").factor == Decimal("3600000")
    assert parse_unit("kPa").factor == Decimal("1000")
    # A kW value converts to W without binary float drift.
    value = Decimal("0.02")
    assert parse_unit("kW").to_si(value) == Decimal("20")
    assert parse_unit("W").from_si(Decimal("20")) == Decimal("20")


def test_compound_unit_implicit_and_explicit_powers():
    flow = parse_unit("m3/h")
    assert flow.dimension == (("length", 3), ("time", -1))
    assert flow.factor == Decimal(1) / Decimal(3600)
    explicit = parse_unit("m^3/s")
    assert explicit.dimension == flow.dimension


def test_dimensionless_unit():
    unit = parse_unit("")
    assert unit.is_dimensionless
    assert unit.factor == Decimal(1)
    assert parse_unit("1").is_dimensionless


def test_affine_temperature_units():
    celsius = parse_unit("degC")
    assert celsius.is_affine
    assert celsius.to_si(Decimal(0)) == Decimal("273.15")
    fahrenheit = parse_unit("degF")
    assert fahrenheit.is_affine
    assert fahrenheit.to_si(Decimal(32)) == Decimal("273.15")
    assert fahrenheit.to_si(Decimal(212)) == Decimal("373.15")


@pytest.mark.parametrize("text", ["degC^2", "degC*m", "m/degC", "degC/s"])
def test_affine_units_rejected_in_compound_expressions(text):
    with pytest.raises(UnitError):
        parse_unit(text)


def test_prefixed_affine_unit_rejected():
    with pytest.raises(UnitError):
        parse_unit("kdegC")


def test_unknown_unit_rejected():
    with pytest.raises(UnitError):
        parse_unit("frobnicate")


def test_same_dimension_different_unit():
    assert same_dimension(parse_unit("W"), parse_unit("kW"))
    assert not same_dimension(parse_unit("W"), parse_unit("V"))


def test_dimension_tuples_are_hashable_and_stable():
    first = parse_unit("kg*m/s^2")
    second = parse_unit("N")
    assert first.dimension == second.dimension
    assert hash(first.dimension) == hash(second.dimension)
