"""Offline virtual (derived) measurement points.

A derived contract declares a directed acyclic graph of arithmetic expressions
over raw contract points and upstream virtual points of the *same device* at
the *same sample timestamp*. Every input carries an absolute error and a unit
dimension. Two explicitly authorized error semantics are supported:

``guaranteed``
    Interval arithmetic. The reported bounds hold for *every* combination of
    inputs inside their declared absolute errors.

``independent``
    First-order (Gaussian) error propagation for statistically independent
    inputs. The reported band is an estimated 1-sigma range; it carries no
    guarantee that the true value must lie inside it.

The two semantics are never mixed under one label: every stored row records
which semantics produced its bounds.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .codec import (
    UnitError,
    UnitSpec,
    combine_dimensions,
    parse_unit,
    quantize_decimal,
    same_dimension,
    sqrt_decimal,
    to_decimal,
)
from .contract import ContractSet
from .util import sha256_text

GUARANTEED = "guaranteed"
INDEPENDENT = "independent"
ERROR_SEMANTICS = (GUARANTEED, INDEPENDENT)

# Quality codes written into derived_readings.quality_code.
DERIVED_GOOD = "derived_good"
DERIVED_INDETERMINATE = "derived_indeterminate"

# Indeterminacy reason codes.
REASON_MISSING = "missing_source"
REASON_BAD_QUALITY = "unacceptable_quality"
REASON_CRC = "crc_check_failed"
REASON_DECODE = "decode_failed"
REASON_CONTRADICTION = "contradictory_values"
REASON_NON_FINITE = "non_finite_value"
REASON_UPSTREAM = "upstream_indeterminate"
REASON_DIVIDE_BY_ZERO = "divisor_interval_crosses_zero"

# Raw-point quality codes that are trustworthy enough to derive from.
ACCEPTABLE_LEAF_QUALITY = {"good", "overflow", "under_range"}

# Frame quality codes that explain why no usable reading exists at a timestamp.
_FRAME_REASON_BY_QUALITY = {
    "bad_crc_request": REASON_CRC,
    "bad_crc_response": REASON_CRC,
    "decode_error": REASON_DECODE,
    "bad_frame": REASON_BAD_QUALITY,
    "exception_response": REASON_BAD_QUALITY,
    "missing_response": REASON_MISSING,
    "under_range": REASON_BAD_QUALITY,
    "overflow": REASON_BAD_QUALITY,
    "not_in_enum": REASON_BAD_QUALITY,
}

_BOUNDS_PLACES = 9


class DerivedConfigError(ValueError):
    """A static derived-contract error; raised before any row is written."""

    def __init__(self, code: str, detail: str, location: str = ""):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.location = location

    def __str__(self) -> str:
        prefix = f"{self.location}: " if self.location else ""
        return f"[{self.code}] {prefix}{self.detail}"


def _fail(code: str, detail: str, location: str = "") -> None:
    raise DerivedConfigError(code, detail, location)


def _require(condition: bool, code: str, detail: str, location: str = "") -> None:
    if not condition:
        _fail(code, detail, location)


# --- Expression AST ----------------------------------------------------------


@dataclass(frozen=True)
class LiteralNode:
    value: Decimal
    text: str


@dataclass(frozen=True)
class RefNode:
    name: str


@dataclass(frozen=True)
class BinaryNode:
    operator: str
    left: Any
    right: Any


Node = LiteralNode | RefNode | BinaryNode

_TOKEN_RE = re.compile(r"\s*([A-Za-z0-9_][A-Za-z0-9_.-]*|[0-9]+(?:\.[0-9]+)?|[()+\-*/])")
_OPERATORS = {"+", "-", "*", "/"}


class _Tokenizer:
    def __init__(self, text: str, location: str):
        self.tokens = _TOKEN_RE.findall(text)
        self.consumed = "".join(self.tokens)
        if self.consumed != re.sub(r"\s+", "", text):
            _fail("derived_expression", f"invalid expression {text!r}", location)
        self.index = 0

    def peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self) -> str:
        token = self.peek()
        self.index += 1
        return token  # type: ignore[return-value]


def _parse_expression(tokenizer: _Tokenizer) -> Node:
    node = _parse_term(tokenizer)
    while tokenizer.peek() in ("+", "-"):
        operator = tokenizer.take()
        node = BinaryNode(operator, node, _parse_term(tokenizer))
    return node


def _parse_term(tokenizer: _Tokenizer) -> Node:
    node = _parse_factor(tokenizer)
    while tokenizer.peek() in ("*", "/"):
        operator = tokenizer.take()
        node = BinaryNode(operator, node, _parse_factor(tokenizer))
    return node


def _parse_factor(tokenizer: _Tokenizer) -> Node:
    token = tokenizer.peek()
    if token is None:
        _fail("derived_expression", "expression ends unexpectedly")
    if token == "(":
        tokenizer.take()
        node = _parse_expression(tokenizer)
        if tokenizer.peek() != ")":
            _fail("derived_expression", "missing closing parenthesis")
        tokenizer.take()
        return node
    tokenizer.take()
    if token in _OPERATORS or token == ")":
        _fail("derived_expression", f"unexpected token {token!r}")
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", token):
        try:
            value = Decimal(token)
        except Exception:  # pragma: no cover - regex already restricts
            _fail("derived_expression", f"invalid number {token!r}")
        if not value.is_finite():
            _fail("derived_expression", f"literal {token!r} is not finite")
        return LiteralNode(value, token)
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", token):
        return RefNode(token)
    _fail("derived_expression", f"invalid token {token!r}")  # pragma: no cover


def parse_expression(text: str, location: str = "<expression>") -> Node:
    _require(isinstance(text, str) and text.strip(),
             "derived_expression", "expression must be a non-empty string", location)
    tokenizer = _Tokenizer(text.strip(), location)
    node = _parse_expression(tokenizer)
    if tokenizer.peek() is not None:
        _fail("derived_expression", f"unexpected trailing token {tokenizer.peek()!r}", location)
    return node


def walk_refs(node: Node) -> set[str]:
    if isinstance(node, LiteralNode):
        return set()
    if isinstance(node, RefNode):
        return {node.name}
    return walk_refs(node.left) | walk_refs(node.right)


# --- Config dataclasses ------------------------------------------------------


@dataclass(frozen=True)
class DerivedInputSpec:
    name: str
    point_key: str
    abs_error: Decimal
    unit: UnitSpec


@dataclass(frozen=True)
class DerivedPointDef:
    device_id: str
    point_key: str
    expression_text: str
    expression: Node
    unit: UnitSpec
    inputs: tuple[DerivedInputSpec, ...]
    error_semantics: str
    expression_version: str
    threshold: Decimal | None
    threshold_direction: str
    ordinal: int

    @property
    def input_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.inputs)


@dataclass(frozen=True)
class DerivedConfig:
    version: int
    default_semantics: str
    points: tuple[DerivedPointDef, ...]
    by_key: dict[str, DerivedPointDef]
    raw_text: str
    fingerprint: str
    topo_keys: tuple[str, ...] = ()

    @property
    def expression_version(self) -> str:
        return "ev_" + self.fingerprint[:16]

    def topological_points(self) -> tuple[DerivedPointDef, ...]:
        """Points in dependency-first order, stable regardless of YAML order."""
        if not self.topo_keys:
            return self.points
        return tuple(self.by_key[key] for key in self.topo_keys)


@dataclass(frozen=True)
class Interval:
    """A closed interval [low, high] in coherent SI units, exact Decimal."""

    low: Decimal
    high: Decimal

    @property
    def crosses_zero(self) -> bool:
        return self.low <= 0 <= self.high


# --- Static validation -------------------------------------------------------


def _parse_inputs(raw: Any, location: str) -> tuple[DerivedInputSpec, ...]:
    _require(isinstance(raw, dict) and raw, "derived_input",
             "inputs must be a non-empty mapping of name -> input spec", location)
    inputs: list[DerivedInputSpec] = []
    seen_points: set[str] = set()
    for name, spec_raw in raw.items():
        item_location = f"{location}.inputs.{name}"
        _require(isinstance(name, str) and re.fullmatch(
            r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", name),
            "derived_input", "input name must match [A-Za-z0-9_][A-Za-z0-9_.-]{0,63}",
            item_location)
        _require(isinstance(spec_raw, dict), "derived_input",
                 "input spec must be a mapping", item_location)
        point_key = spec_raw.get("point")
        _require(isinstance(point_key, str) and point_key,
                 "derived_input", "input.point must name a point key", item_location)
        error_raw = spec_raw.get("abs_error")
        _require(isinstance(error_raw, (int, float, str)) and not isinstance(error_raw, bool),
                 "derived_input", "input.abs_error must be a finite non-negative number",
                 item_location)
        try:
            abs_error = to_decimal(error_raw)
        except Exception:
            _fail("derived_input", f"invalid abs_error {error_raw!r}", item_location)
        _require(abs_error.is_finite() and abs_error >= 0,
                 "derived_input", "input.abs_error must be finite and non-negative",
                 item_location)
        unit_raw = spec_raw.get("unit", "")
        try:
            unit = parse_unit(unit_raw)
        except UnitError as exc:
            _fail("derived_unit", str(exc), item_location)
        _require(point_key not in seen_points, "derived_input",
                 f"duplicate input for point {point_key!r}", item_location)
        seen_points.add(point_key)
        inputs.append(DerivedInputSpec(name=name, point_key=point_key,
                                       abs_error=abs_error, unit=unit))
    return tuple(inputs)


def _validate_dimensions(
    definition: DerivedPointDef,
    contract: ContractSet,
    config: DerivedConfig | None,
    location: str,
) -> None:
    """Resolve the dimension of every node and enforce operation compatibility."""
    inputs_by_name = {item.name: item for item in definition.inputs}
    point_unit_cache: dict[str, UnitSpec] = {}

    def unit_of_point(point_key: str) -> UnitSpec:
        if point_key in point_unit_cache:
            return point_unit_cache[point_key]
        raw_point = contract.get(point_key)
        if raw_point is not None:
            try:
                unit = parse_unit(raw_point.unit)
            except UnitError as exc:
                _fail("derived_unit", f"point {point_key!r}: {exc}", location)
            point_unit_cache[point_key] = unit
            return unit
        if config is not None and point_key in config.by_key:
            unit = config.by_key[point_key].unit
            point_unit_cache[point_key] = unit
            return unit
        _fail("derived_unknown_point",
              f"input references unknown point {point_key!r}", location)

    def resolve(node: Node) -> UnitSpec:
        if isinstance(node, LiteralNode):
            return parse_unit("")
        if isinstance(node, RefNode):
            if node.name in inputs_by_name:
                spec = inputs_by_name[node.name]
                actual = unit_of_point(spec.point_key)
                _require(same_dimension(spec.unit, actual),
                         "derived_unit_mismatch",
                         f"input {node.name!r} declares unit {spec.unit.label!r} but "
                         f"point {spec.point_key!r} has unit {actual.label!r}",
                         location)
                return spec.unit
            _fail("derived_unknown_input",
                  f"expression references {node.name!r} which is not declared in inputs",
                  location)
        assert isinstance(node, BinaryNode)
        left = resolve(node.left)
        right = resolve(node.right)
        if node.operator in ("+", "-"):
            _require(same_dimension(left, right), "derived_dimension",
                     f"cannot {node.operator} incompatible dimensions "
                     f"{left.dimension} and {right.dimension}", location)
            if left.is_affine or right.is_affine:
                # Only a temperature delta on one identical affine scale is
                # meaningful; the result is an offset-free delta.
                _require(node.operator == "-" and left.label == right.label
                         and left.factor == right.factor,
                         "derived_affine",
                         "offset units (e.g. degC) only support subtraction of two "
                         "values on the identical scale, producing an offset-free delta",
                         location)
                return UnitSpec(f"delta_{left.label}", left.factor, Decimal(0),
                                left.dimension)
            return left
        # Multiplication / division.
        _require(not left.is_affine and not right.is_affine,
                 "derived_affine",
                 "offset units (e.g. degC) must be converted to an absolute scale "
                 "(e.g. K) before multiplication or division", location)
        dimension = combine_dimensions(left, right, node.operator == "*")
        return UnitSpec("", Decimal(1), Decimal(0), dimension)

    result_dimension = resolve(definition.expression)
    _require(same_dimension(result_dimension, definition.unit),
             "derived_output_unit",
             f"declared output unit {definition.unit.label!r} has dimension "
             f"{definition.unit.dimension} but the expression yields {result_dimension.dimension}",
             location)


def _topological_order(
    definitions: list[DerivedPointDef], location: str
) -> list[DerivedPointDef]:
    """Return definitions in dependency-first DAG order; reject any cycle."""
    by_key = {item.point_key: item for item in definitions}
    keys = set(by_key)
    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[DerivedPointDef] = []

    def visit(key: str, stack: tuple[str, ...]) -> None:
        if key in visited:
            return
        if key in visiting:
            cycle = " -> ".join(stack + (key,))
            _fail("derived_cycle",
                  f"expression graph contains a cycle: {cycle}", location)
        visiting.add(key)
        for spec in by_key[key].inputs:
            if spec.point_key in keys:
                visit(spec.point_key, stack + (key,))
        visiting.discard(key)
        visited.add(key)
        ordered.append(by_key[key])

    for definition in definitions:
        visit(definition.point_key, ())
    return ordered


def _validate_semantics_consistency(
    config: DerivedConfig, location: str
) -> None:
    """A connected expression chain must use one authorized error semantics.

    Mixing a guaranteed interval node with a 1-sigma node inside one chain
    would either crash (sigma is undefined for a guarantee) or mislabel a
    bound; reject it explicitly instead of silently converting the two.
    Different, disconnected points may still choose different semantics.
    """
    by_key = config.by_key
    component_semantics: dict[str, str] = {}

    def root_semantics(key: str) -> str:
        definition = by_key[key]
        for spec in definition.inputs:
            if spec.point_key in by_key:
                return root_semantics(spec.point_key)
        return definition.error_semantics

    for definition in config.topological_points():
        upstream = {
            root_semantics(spec.point_key)
            for spec in definition.inputs if spec.point_key in by_key
        }
        if upstream and definition.error_semantics not in upstream:
            _fail(
                "derived_semantics_mix",
                f"point {definition.point_key!r} uses {definition.error_semantics!r} "
                f"but its upstream chain uses {sorted(upstream)!r}; a chain must use "
                "one error semantics end to end",
                location,
            )
        component_semantics[definition.point_key] = definition.error_semantics


def _parse_point_definition(
    raw: Any,
    index: int,
    source: str,
    default_semantics: str,
    contract: ContractSet,
) -> DerivedPointDef:
    location = f"{source}:derived_points[{index}]"
    _require(isinstance(raw, dict), "derived_point",
             "derived point must be a mapping", location)
    device_id = raw.get("device_id")
    _require(isinstance(device_id, str) and device_id,
             "derived_point", "device_id is required", location)
    _require(any(device.device_id == device_id for device in contract.devices),
             "derived_unknown_device", f"unknown device {device_id!r}", location)
    point_key = raw.get("key")
    _require(isinstance(point_key, str) and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", point_key),
        "derived_point", "point key must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}", location)
    _require(contract.get(point_key) is None, "derived_duplicate_key",
             f"derived key {point_key!r} collides with a raw contract point", location)
    expression_text = raw.get("expression")
    expression = parse_expression(expression_text, location)
    inputs = _parse_inputs(raw.get("inputs"), location)
    for spec in inputs:
        target_contract = contract.get(spec.point_key)
        if target_contract is not None:
            _require(target_contract.device_id == device_id, "derived_cross_device",
                     f"input {spec.name!r} points at {spec.point_key!r} on device "
                     f"{target_contract.device_id!r}, expected {device_id!r}", location)
    unit_raw = raw.get("unit", "")
    try:
        unit = parse_unit(unit_raw)
    except UnitError as exc:
        _fail("derived_unit", str(exc), location)
    semantics = raw.get("error_semantics", default_semantics)
    _require(semantics in ERROR_SEMANTICS, "derived_semantics",
             f"error_semantics must be one of {', '.join(ERROR_SEMANTICS)}", location)
    threshold = None
    threshold_direction = ""
    if "threshold" in raw:
        threshold_raw = raw["threshold"]
        _require(isinstance(threshold_raw, dict), "derived_threshold",
                 "threshold must be a mapping {direction, value}", location)
        threshold_direction = threshold_raw.get("direction", "")
        _require(threshold_direction in ("above", "below"),
                 "derived_threshold", "threshold.direction must be 'above' or 'below'",
                 location)
        try:
            threshold = to_decimal(threshold_raw.get("value"))
        except Exception:
            _fail("derived_threshold", "threshold.value must be a finite number", location)
        _require(threshold.is_finite(), "derived_threshold",
                 "threshold.value must be finite", location)
    return DerivedPointDef(
        device_id=device_id,
        point_key=point_key,
        expression_text=expression_text.strip(),
        expression=expression,
        unit=unit,
        inputs=inputs,
        error_semantics=semantics,
        expression_version="",
        threshold=threshold,
        threshold_direction=threshold_direction,
        ordinal=index,
    )


def parse_derived_config(text: str, contract: ContractSet,
                         source: str = "<memory>") -> DerivedConfig:
    """Parse and fully validate a derived-point contract document."""
    import yaml

    try:
        root = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        _fail("derived_yaml", f"invalid YAML: {exc}", source)
    _require(isinstance(root, dict), "derived_root",
             "derived contract root must be a mapping", source)
    version = root.get("version")
    _require(version == 1, "derived_version", "version must be 1", source)
    default_semantics = root.get("error_semantics", GUARANTEED)
    _require(default_semantics in ERROR_SEMANTICS, "derived_semantics",
             f"top-level error_semantics must be one of {', '.join(ERROR_SEMANTICS)}",
             source)
    raw_points = root.get("derived_points")
    _require(isinstance(raw_points, list) and raw_points, "derived_points",
             "derived_points must be a non-empty list", source)

    definitions = [
        _parse_point_definition(raw, index, source, default_semantics, contract)
        for index, raw in enumerate(raw_points)
    ]
    seen: set[str] = set()
    for definition in definitions:
        _require(definition.point_key not in seen, "derived_duplicate_key",
                 f"duplicate derived point key {definition.point_key!r}",
                 f"{source}:derived_points[{definition.ordinal}]")
        seen.add(definition.point_key)

    fingerprint = sha256_text(text)
    expression_version = "ev_" + fingerprint[:16]
    finalized = tuple(
        DerivedPointDef(
            device_id=item.device_id, point_key=item.point_key,
            expression_text=item.expression_text, expression=item.expression,
            unit=item.unit, inputs=item.inputs, error_semantics=item.error_semantics,
            expression_version=expression_version, threshold=item.threshold,
            threshold_direction=item.threshold_direction, ordinal=item.ordinal,
        )
        for item in definitions
    )
    # Cycle rejection and dependency-first order in one DFS pass.
    topo = _topological_order(list(finalized), source)
    topo_keys = tuple(item.point_key for item in topo)
    config = DerivedConfig(
        version=1,
        default_semantics=default_semantics,
        points=finalized,
        by_key={item.point_key: item for item in finalized},
        raw_text=text,
        fingerprint=fingerprint,
        topo_keys=topo_keys,
    )
    # Dimension resolution needs the full graph so upstream derived units resolve.
    for definition in finalized:
        location = f"{source}:derived_points[{definition.ordinal}]"
        _validate_dimensions(definition, contract, config, location)
    # Static divisor zero-crossing check uses declared ranges and errors.
    for definition in finalized:
        location = f"{source}:derived_points[{definition.ordinal}]"
        _validate_divisor_bounds(definition, contract, config, location)
    _validate_semantics_consistency(config, source)
    return config


def load_derived_config(path: str, contract: ContractSet) -> DerivedConfig:
    from pathlib import Path

    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        _fail("derived_read", f"cannot read derived contract: {exc}", str(target))
    return parse_derived_config(text, contract, str(target))


# --- Static divisor zero-crossing analysis ----------------------------------


class _DerivedDivisorZero(Exception):
    """Internal signal: a statically analyzed divisor interval crosses zero."""


def _guaranteed_input_interval(
    point_key: str,
    contract: ContractSet,
    config: DerivedConfig,
    cache: dict[str, Interval | None],
) -> Interval | None:
    """Widest guaranteed SI interval reachable by a point from static metadata.

    Returns ``None`` when the point is a derived node whose interval cannot be
    bounded statically (upstream of an unconstrained input).
    """
    if point_key in cache:
        return cache[point_key]
    raw_point = contract.get(point_key)
    if raw_point is not None:
        unit = parse_unit(raw_point.unit)
        low = unit.to_si(to_decimal(raw_point.range_min))
        high = unit.to_si(to_decimal(raw_point.range_max))
        cache[point_key] = Interval(low, high)
        return cache[point_key]
    definition = config.by_key.get(point_key)
    if definition is None:
        cache[point_key] = None
        return None
    interval = _static_expression_interval(definition.expression, definition,
                                           contract, config, cache)
    cache[point_key] = interval
    return interval


def _static_expression_interval(
    node: Node,
    definition: DerivedPointDef,
    contract: ContractSet,
    config: DerivedConfig,
    cache: dict[str, Interval | None],
) -> Interval | None:
    if isinstance(node, LiteralNode):
        return Interval(node.value, node.value)
    if isinstance(node, RefNode):
        spec = next(item for item in definition.inputs if item.name == node.name)
        base = _guaranteed_input_interval(spec.point_key, contract, config, cache)
        if base is None:
            return None
        error_si = spec.abs_error * spec.unit.factor
        return Interval(base.low - error_si, base.high + error_si)
    assert isinstance(node, BinaryNode)
    left = _static_expression_interval(node.left, definition, contract, config, cache)
    right = _static_expression_interval(node.right, definition, contract, config, cache)
    if left is None or right is None:
        return None
    if node.operator == "+":
        return Interval(left.low + right.low, left.high + right.high)
    if node.operator == "-":
        return Interval(left.low - right.high, left.high - right.low)
    if node.operator == "*":
        corners = (left.low * right.low, left.low * right.high,
                   left.high * right.low, left.high * right.high)
        return Interval(min(corners), max(corners))
    if right.crosses_zero:
        # A divisor whose allowed interval includes zero makes the quotient
        # unbounded; surface it distinctly from "not statically bounded".
        raise _DerivedDivisorZero()
    corners = (left.low / right.low, left.low / right.high,
               left.high / right.low, left.high / right.high)
    return Interval(min(corners), max(corners))


def _validate_divisor_bounds(
    definition: DerivedPointDef,
    contract: ContractSet,
    config: DerivedConfig,
    location: str,
) -> None:
    cache: dict[str, Interval | None] = {}

    def walk(node: Node) -> None:
        if isinstance(node, BinaryNode):
            walk(node.left)
            walk(node.right)
            if node.operator == "/":
                try:
                    interval = _static_expression_interval(
                        node.right, definition, contract, config, cache
                    )
                except _DerivedDivisorZero:
                    _fail("derived_divide_by_zero",
                          "divisor interval includes zero for some allowed input; "
                          "tighten the range or error declaration", location)
                if interval is not None and interval.crosses_zero:
                    _fail("derived_divide_by_zero",
                          "divisor interval includes zero for some allowed input; "
                          "tighten the range or error declaration", location)

    walk(definition.expression)


# --- Runtime value model -----------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    point_key: str
    line_no: int
    quality_code: str

    def to_dict(self) -> dict[str, Any]:
        return {"point_key": self.point_key, "line_no": self.line_no,
                "quality_code": self.quality_code}


@dataclass(frozen=True)
class LeafSample:
    """One accepted raw reading aligned to a derived evaluation timestamp."""

    point_key: str
    line_no: int
    value_si: Decimal
    error_si: Decimal
    quality_code: str
    unit: UnitSpec


@dataclass
class DValue:
    """Evaluation result in coherent SI units."""

    ok: bool
    reasons: tuple[str, ...] = ()
    value: Decimal | None = None
    low: Decimal | None = None
    high: Decimal | None = None
    sigma: Decimal | None = None
    sources: tuple[SourceRef, ...] = ()
    unit: UnitSpec | None = None
    # A delta of two affine scales (e.g. degC difference) stays offset-free:
    # converting it back to degC must divide by the factor without applying
    # the 273.15 affine offset.
    is_delta: bool = False

    @staticmethod
    def failed(reasons: tuple[str, ...], sources: tuple[SourceRef, ...]) -> "DValue":
        return DValue(ok=False, reasons=tuple(dict.fromkeys(reasons)), sources=sources)


SEMANTICS_DESCRIPTION = {
    GUARANTEED: "guaranteed bounds: interval arithmetic, holds for every allowed input",
    INDEPENDENT: "independent 1-sigma estimate: first-order propagation of independent "
                 "errors; not a guarantee that the true value lies inside the band",
}


# --- Runtime interval arithmetic ---------------------------------------------


def _interval_add(left: Interval, right: Interval) -> Interval:
    return Interval(left.low + right.low, left.high + right.high)


def _interval_sub(left: Interval, right: Interval) -> Interval:
    return Interval(left.low - right.high, left.high - right.low)


def _interval_mul(left: Interval, right: Interval) -> Interval:
    corners = (left.low * right.low, left.low * right.high,
               left.high * right.low, left.high * right.high)
    return Interval(min(corners), max(corners))


def _interval_div(left: Interval, right: Interval) -> Interval:
    corners = (left.low / right.low, left.low / right.high,
               left.high / right.low, left.high / right.high)
    return Interval(min(corners), max(corners))


def _guaranteed_evaluate(node: Node, values: dict[str, DValue]) -> DValue:
    if isinstance(node, LiteralNode):
        point = Interval(node.value, node.value)
        return DValue(ok=True, value=node.value, low=point.low, high=point.high,
                      sigma=Decimal(0), sources=(), unit=parse_unit(""))
    if isinstance(node, RefNode):
        return values[node.name]
    left = _guaranteed_evaluate(node.left, values)
    right = _guaranteed_evaluate(node.right, values)
    sources = tuple(dict.fromkeys(left.sources + right.sources))
    if not left.ok or not right.ok:
        reasons = tuple(dict.fromkeys(left.reasons + right.reasons
                                      + ((REASON_UPSTREAM,) if not (left.ok and right.ok) else ())))
        return DValue.failed(reasons, sources)
    assert left.low is not None and right.low is not None
    is_delta = False
    if node.operator == "+":
        result = _interval_add(Interval(left.low, left.high), Interval(right.low, right.high))
        center = _interval_center(node.operator, left, right)
    elif node.operator == "-":
        result = _interval_sub(Interval(left.low, left.high), Interval(right.low, right.high))
        center = _interval_center(node.operator, left, right)
        is_delta = left.unit.is_affine or right.unit.is_affine
    elif node.operator == "*":
        result = _interval_mul(Interval(left.low, left.high), Interval(right.low, right.high))
        center = _interval_center(node.operator, left, right)
    else:
        right_interval = Interval(right.low, right.high)
        if right_interval.crosses_zero:
            return DValue.failed((REASON_DIVIDE_BY_ZERO,), sources)
        result = _interval_div(Interval(left.low, left.high), right_interval)
        center = _interval_center(node.operator, left, right)
    dimension = combine_dimensions(left.unit, right.unit, node.operator in ("*",)) \
        if node.operator in ("*", "/") else left.unit.dimension
    return DValue(ok=True, value=center, low=result.low, high=result.high,
                  sigma=None, sources=sources, is_delta=is_delta,
                  unit=UnitSpec("", Decimal(1), Decimal(0), dimension))


def _interval_center(operator: str, left: DValue, right: DValue) -> Decimal:
    lc, rc = left.value, right.value
    if operator == "+":
        return lc + rc
    if operator == "-":
        return lc - rc
    if operator == "*":
        return lc * rc
    return lc / rc


def _independent_evaluate(node: Node, values: dict[str, DValue]) -> DValue:
    """First-order propagation: sigma_y^2 = sum (df/dx_i * sigma_i)^2."""
    if isinstance(node, LiteralNode):
        return DValue(ok=True, value=node.value, low=node.value, high=node.value,
                      sigma=Decimal(0), sources=(), unit=parse_unit(""))
    if isinstance(node, RefNode):
        return values[node.name]
    left = _independent_evaluate(node.left, values)
    right = _independent_evaluate(node.right, values)
    sources = tuple(dict.fromkeys(left.sources + right.sources))
    if not left.ok or not right.ok:
        reasons = tuple(dict.fromkeys(left.reasons + right.reasons + (REASON_UPSTREAM,)))
        return DValue.failed(reasons, sources)
    lc, rc, sl, sr = left.value, right.value, left.sigma, right.sigma
    is_delta = False
    if node.operator == "+":
        center, sigma = lc + rc, sqrt_decimal(sl ** 2 + sr ** 2)
    elif node.operator == "-":
        center, sigma = lc - rc, sqrt_decimal(sl ** 2 + sr ** 2)
        is_delta = left.unit.is_affine or right.unit.is_affine
    elif node.operator == "*":
        center = lc * rc
        sigma = sqrt_decimal((rc * sl) ** 2 + (lc * sr) ** 2)
    else:
        if Interval(rc - sr, rc + sr).crosses_zero:
            return DValue.failed((REASON_DIVIDE_BY_ZERO,), sources)
        center = lc / rc
        sigma = sqrt_decimal((sl / rc) ** 2 + ((lc * sr) / (rc ** 2)) ** 2)
    dimension = combine_dimensions(left.unit, right.unit, node.operator in ("*",)) \
        if node.operator in ("*", "/") else left.unit.dimension
    return DValue(ok=True, value=center, low=center - sigma, high=center + sigma,
                  sigma=sigma, sources=sources, is_delta=is_delta,
                  unit=UnitSpec("", Decimal(1), Decimal(0), dimension))



# --- Sample alignment --------------------------------------------------------


@dataclass(frozen=True)
class AlignedSample:
    """One accepted raw reading aligned at a sample timestamp, in SI units."""

    point_key: str
    line_no: int
    value_si: Decimal
    error_si: Decimal
    quality_code: str
    unit: UnitSpec


@dataclass(frozen=True)
class AlignedPoint:
    """All readings of one point keyed by timestamp, with rejection reasons."""

    samples: dict[str, AlignedSample]
    failures: dict[str, tuple[str, ...]]


def _rows_for_point(store: Any, batch_id: str, device_id: str,
                    point_key: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    readings = [dict(row) for row in store.conn.execute(
        "SELECT line_no, ts, engineering_value, quality_code "
        "FROM readings WHERE batch_id = ? AND device_id = ? AND point_key = ? "
        "ORDER BY ts, line_no, id",
        (batch_id, device_id, point_key),
    ).fetchall()]
    # Frames explain timestamps where CRC/decode/frame failures prevented a
    # reading from ever being written.
    frames = [dict(row) for row in store.conn.execute(
        "SELECT line_no, ts, quality_code FROM frames "
        "WHERE batch_id = ? AND device_id = ? AND point_key = ? AND contract_matched = 1 "
        "ORDER BY ts, line_no",
        (batch_id, device_id, point_key),
    ).fetchall()]
    return readings, frames


def align_samples(
    rows: list[dict[str, Any]],
    point_key: str,
    unit: UnitSpec,
    abs_error: Decimal,
    frames: list[dict[str, Any]] | None = None,
) -> AlignedPoint:
    """Group raw readings by timestamp.

    Rejects non-finite values, unacceptable quality codes and contradictory
    values for the same (point, timestamp); each rejection keeps an explicit
    reason code instead of silently dropping the timestamp. Failed frames
    (CRC/decode/...) contribute their own timestamps and reasons so a virtual
    point reports the concrete cause rather than a generic "missing".
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["ts"], []).append(row)
    frame_by_ts: dict[str, list[dict[str, Any]]] = {}
    for frame in frames or []:
        frame_by_ts.setdefault(frame["ts"], []).append(frame)
    samples: dict[str, AlignedSample] = {}
    failures: dict[str, tuple[str, ...]] = {}
    error_si = abs_error * unit.factor
    timestamps = set(grouped) | set(frame_by_ts)
    for ts in timestamps:
        members = grouped.get(ts, [])
        frame_members = frame_by_ts.get(ts, [])
        reasons: list[str] = []
        for frame in frame_members:
            reason = _FRAME_REASON_BY_QUALITY.get(frame["quality_code"],
                                                  REASON_BAD_QUALITY)
            reasons.append(reason)
        usable: list[dict[str, Any]] = []
        for member in members:
            value = member["engineering_value"]
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                reasons.append(REASON_NON_FINITE)
                continue
            if member["quality_code"] not in ACCEPTABLE_LEAF_QUALITY:
                reasons.append(REASON_BAD_QUALITY)
                continue
            usable.append(member)
        if not usable:
            failures[ts] = tuple(dict.fromkeys(reasons)) or (REASON_MISSING,)
            continue
        values_si = {
            unit.to_si(to_decimal(member["engineering_value"])) for member in usable
        }
        if len(values_si) > 1:
            failures[ts] = tuple(dict.fromkeys([*reasons, REASON_CONTRADICTION]))
            continue
        chosen = usable[0]
        samples[ts] = AlignedSample(
            point_key=point_key,
            line_no=int(chosen["line_no"]),
            value_si=next(iter(values_si)),
            error_si=error_si,
            quality_code=chosen["quality_code"],
            unit=unit,
        )
    return AlignedPoint(samples=samples, failures=failures)


# --- Per-timestamp evaluation ------------------------------------------------


def _leaf_dvalue(sample: AlignedSample) -> DValue:
    return DValue(
        ok=True,
        value=sample.value_si,
        low=sample.value_si - sample.error_si,
        high=sample.value_si + sample.error_si,
        sigma=sample.error_si,
        sources=(SourceRef(sample.point_key, sample.line_no, sample.quality_code),),
        unit=sample.unit,
    )


def _upstream_dvalue(row: dict[str, Any]) -> DValue:
    sources = tuple(
        SourceRef(item["point_key"], int(item["line_no"]), item["quality_code"])
        for item in json.loads(row["sources_json"])
    )
    if row["quality_code"] != DERIVED_GOOD:
        return DValue.failed((REASON_UPSTREAM,), sources)
    value = to_decimal(row["engineering_value_si"])
    low = to_decimal(row["lower_bound_si"])
    high = to_decimal(row["upper_bound_si"])
    sigma = to_decimal(row["sigma_si"]) if row["sigma_si"] is not None else None
    return DValue(ok=True, value=value, low=low, high=high, sigma=sigma,
                  sources=sources, unit=parse_unit(row["unit"]),
                  is_delta=bool(row.get("is_delta")))


def _classify_threshold(
    definition: DerivedPointDef,
    low_si: Decimal,
    high_si: Decimal,
    is_delta: bool,
) -> str:
    if definition.threshold is None:
        return ""
    if is_delta:
        # Threshold on a temperature delta uses the offset-free scale.
        threshold_si = definition.threshold * definition.unit.factor
    else:
        threshold_si = definition.unit.to_si(definition.threshold)
    direction = definition.threshold_direction
    if direction == "above":
        if low_si > threshold_si:
            return "above"
        if high_si > threshold_si:
            return "possibly_above"
        return "within"
    # below
    if high_si < threshold_si:
        return "below"
    if low_si < threshold_si:
        return "possibly_below"
    return "within"


def _evaluate_timestamp(
    definition: DerivedPointDef,
    ts: str,
    aligned: dict[str, AlignedPoint],
    upstream: dict[str, dict[str, dict[str, Any]]],
) -> DValue:
    values: dict[str, DValue] = {}
    failure_reasons: list[str] = []
    failure_sources: list[SourceRef] = []
    for spec in definition.inputs:
        if spec.point_key in upstream:
            # Upstream virtual point: chain its stored value, bounds and
            # provenance instead of treating it as a raw aligned sample.
            rows = upstream[spec.point_key]
            if ts not in rows:
                failure_reasons.append(REASON_MISSING)
                continue
            dvalue = _upstream_dvalue(rows[ts])
            if not dvalue.ok:
                failure_reasons.extend(dvalue.reasons)
                failure_sources.extend(dvalue.sources)
            values[spec.name] = dvalue
            continue
        point = aligned[spec.name]
        if ts in point.failures:
            failure_reasons.extend(point.failures[ts])
            continue
        sample = point.samples.get(ts)
        if sample is None:
            failure_reasons.append(REASON_MISSING)
            continue
        values[spec.name] = _leaf_dvalue(sample)
        failure_sources.append(SourceRef(
            sample.point_key, sample.line_no, sample.quality_code))
    if failure_reasons:
        return DValue.failed(
            tuple(dict.fromkeys(failure_reasons)),
            tuple(dict.fromkeys(failure_sources)),
        )
    evaluator = (_guaranteed_evaluate if definition.error_semantics == GUARANTEED
                 else _independent_evaluate)
    return evaluator(definition.expression, values)


# --- Materialization ---------------------------------------------------------


def make_derived_contract_id(fingerprint: str) -> str:
    return f"d_{fingerprint[:16]}"


def register_derived_contract(
    store: Any,
    config: DerivedConfig,
    source_path: str,
    now: str,
) -> str:
    """Persist the static contract document and its point definitions."""
    contract_id = make_derived_contract_id(config.fingerprint)
    with store.conn:
        store.conn.execute(
            "INSERT OR IGNORE INTO derived_contracts("
            "id, source_path, sha256, version, error_semantics, "
            "expression_version, created_at) VALUES(?,?,?,?,?,?,?)",
            (contract_id, source_path, config.fingerprint, config.version,
             config.default_semantics, config.expression_version, now),
        )
        for definition in config.points:
            store.conn.execute(
                "INSERT OR IGNORE INTO derived_points("
                "contract_id, device_id, point_key, expression, expression_version, "
                "unit, dimension_json, error_semantics, inputs_json, threshold, "
                "threshold_direction, ordinal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (contract_id, definition.device_id, definition.point_key,
                 definition.expression_text, definition.expression_version,
                 definition.unit.label,
                 json.dumps([list(item) for item in definition.unit.dimension]),
                 definition.error_semantics,
                 json.dumps([
                     {"name": item.name, "point": item.point_key,
                      "abs_error": str(item.abs_error), "unit": item.unit.label}
                     for item in definition.inputs
                 ]),
                 float(definition.threshold) if definition.threshold is not None else None,
                 definition.threshold_direction, definition.ordinal),
            )
    return contract_id


def _from_output_unit(unit: UnitSpec, value_si: Decimal, is_delta: bool) -> Decimal:
    """Convert a coherent SI value into the declared output unit.

    Deltas (e.g. a degC temperature difference) bypass the affine offset.
    """
    if is_delta:
        return value_si / unit.factor
    return unit.from_si(value_si)


def _store_derived_reading(
    store: Any,
    *,
    contract_id: str,
    batch_id: str,
    definition: DerivedPointDef,
    ts: str,
    result: DValue,
) -> None:
    unit = definition.unit
    sources = [item.to_dict() for item in result.sources]
    columns = (
        "contract_id, batch_id, device_id, point_key, ts, expression_version, "
        "error_semantics, quality_code, reason_codes, engineering_value, unit, "
        "lower_bound, upper_bound, bound_sigma, threshold, threshold_state, "
        "sources_json, engineering_value_si, lower_bound_si, upper_bound_si, "
        "sigma_si, is_delta, line_no_json"
    )
    placeholders = ",".join(("?",) * 23)
    threshold_value = (
        float(definition.threshold) if definition.threshold is not None else None
    )
    line_numbers = sorted({item.line_no for item in result.sources if item.line_no})
    if not result.ok:
        store.conn.execute(
            f"INSERT OR IGNORE INTO derived_readings({columns}) "
            f"VALUES({placeholders})",
            (contract_id, batch_id, definition.device_id, definition.point_key, ts,
             definition.expression_version, definition.error_semantics,
             DERIVED_INDETERMINATE, ",".join(result.reasons), None, unit.label,
             None, None, "", threshold_value, "", json.dumps(sources),
             None, None, None, None, int(result.is_delta),
             json.dumps(line_numbers)),
        )
        return
    value = _from_output_unit(unit, result.value, result.is_delta)
    low = _from_output_unit(unit, result.low, result.is_delta)
    high = _from_output_unit(unit, result.high, result.is_delta)
    threshold_state = _classify_threshold(
        definition, result.low, result.high, result.is_delta)
    bound_sigma = ""
    sigma_si = None
    if definition.error_semantics == INDEPENDENT:
        bound_sigma = "1sigma"
        sigma_si = str(quantize_decimal(result.sigma, _BOUNDS_PLACES))
    store.conn.execute(
        f"INSERT OR IGNORE INTO derived_readings({columns}) VALUES({placeholders})",
        (contract_id, batch_id, definition.device_id, definition.point_key, ts,
         definition.expression_version, definition.error_semantics, DERIVED_GOOD,
         "", float(quantize_decimal(value, _BOUNDS_PLACES)), unit.label,
         float(quantize_decimal(low, _BOUNDS_PLACES)),
         float(quantize_decimal(high, _BOUNDS_PLACES)), bound_sigma,
         threshold_value, threshold_state, json.dumps(sources),
         str(quantize_decimal(result.value, _BOUNDS_PLACES)),
         str(quantize_decimal(result.low, _BOUNDS_PLACES)),
         str(quantize_decimal(result.high, _BOUNDS_PLACES)),
         sigma_si, int(result.is_delta), json.dumps(line_numbers)),
    )


@dataclass
class MaterializeResult:
    contract_id: str
    rows_written: int
    good_rows: int
    indeterminate_rows: int
    skipped_existing: int


def materialize_derived(
    store: Any,
    batch_id: str,
    contract: ContractSet,
    config: DerivedConfig,
    source_path: str,
    *,
    now: str,
) -> MaterializeResult:
    """Evaluate every derived point for one completed batch, topologically.

    Raw ``readings`` are never modified; every output lands in
    ``derived_readings`` with expression version, source line numbers, input
    quality, value, unit and error bounds. Repeated calls write nothing twice.
    """
    contract_id = register_derived_contract(store, config, source_path, now)
    rows_written = good_rows = indeterminate_rows = skipped = 0
    ordered = config.topological_points()
    derived_keys = {item.point_key for item in ordered}
    with store.conn:
        for definition in ordered:
            aligned: dict[str, AlignedPoint] = {}
            all_timestamps: set[str] = set()
            for spec in definition.inputs:
                if spec.point_key in derived_keys:
                    continue
                rows, frames = _rows_for_point(store, batch_id, definition.device_id,
                                               spec.point_key)
                point = align_samples(rows, spec.point_key, spec.unit, spec.abs_error,
                                      frames=frames)
                aligned[spec.name] = point
                all_timestamps.update(point.samples, point.failures)
            upstream: dict[str, dict[str, dict[str, Any]]] = {}
            for spec in definition.inputs:
                if spec.point_key not in derived_keys:
                    continue
                rows = store.conn.execute(
                    "SELECT ts, engineering_value_si, lower_bound_si, upper_bound_si, "
                    "sigma_si, quality_code, unit, sources_json, is_delta "
                    "FROM derived_readings "
                    "WHERE contract_id = ? AND batch_id = ? AND device_id = ? "
                    "AND point_key = ?",
                    (contract_id, batch_id, definition.device_id, spec.point_key),
                ).fetchall()
                upstream[spec.point_key] = {row["ts"]: dict(row) for row in rows}
                all_timestamps.update(upstream[spec.point_key])
                # Upstream nodes feed through their aligned input names too.
                aligned[spec.name] = AlignedPoint(samples={}, failures={})
            for ts in sorted(all_timestamps):
                existing = store.conn.execute(
                    "SELECT 1 FROM derived_readings WHERE contract_id = ? AND batch_id = ? "
                    "AND device_id = ? AND point_key = ? AND ts = ?",
                    (contract_id, batch_id, definition.device_id,
                     definition.point_key, ts),
                ).fetchone()
                if existing is not None:
                    skipped += 1
                    continue
                result = _evaluate_timestamp(definition, ts, aligned, upstream)
                _store_derived_reading(
                    store, contract_id=contract_id, batch_id=batch_id,
                    definition=definition, ts=ts, result=result,
                )
                rows_written += 1
                if result.ok:
                    good_rows += 1
                else:
                    indeterminate_rows += 1
    return MaterializeResult(
        contract_id=contract_id,
        rows_written=rows_written,
        good_rows=good_rows,
        indeterminate_rows=indeterminate_rows,
        skipped_existing=skipped,
    )
