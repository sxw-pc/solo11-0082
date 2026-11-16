"""Deterministic register decoding, SI unit bookkeeping and exact Decimal math.

The unit system models the physical dimensions used on site (voltage, current,
power, energy, temperature, ...) with a map of base-dimension exponents. SI
prefixes (``kW`` vs ``W``) become exact :class:`decimal.Decimal` factors and
offset units such as ``degC`` are flagged affine so the derived-point engine
can refuse to multiply them without an explicit conversion to an absolute
scale (Kelvin).
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from decimal import Decimal, getcontext, localcontext

getcontext().prec = 50

# --- Units and dimensions ---------------------------------------------------

# Base dimension keys. Dimension maps are frozen tuples sorted by key so they
# hash deterministically.
_BASE_DIMENSIONS = (
    "amount",       # mol
    "current",      # A
    "length",       # m
    "luminous",     # cd
    "mass",         # kg
    "temperature",  # K
    "time",         # s
)

# name -> (SI factor as Decimal text, dimension exponent map, affine offset)
# The factor converts a value expressed in the unit into the coherent SI unit
# of its dimension: value_si = value * factor + offset.
_UNIT_TABLE: dict[str, tuple[str, dict[str, int], str]] = {
    # Coherent base units.
    "mol": ("1", {"amount": 1}, "0"),
    "A": ("1", {"current": 1}, "0"),
    "m": ("1", {"length": 1}, "0"),
    "cm": ("0.01", {"length": 1}, "0"),
    "mm": ("0.001", {"length": 1}, "0"),
    "km": ("1000", {"length": 1}, "0"),
    "cd": ("1", {"luminous": 1}, "0"),
    "kg": ("1", {"mass": 1}, "0"),
    "g": ("0.001", {"mass": 1}, "0"),
    "K": ("1", {"temperature": 1}, "0"),
    "s": ("1", {"time": 1}, "0"),
    "min": ("60", {"time": 1}, "0"),
    "h": ("3600", {"time": 1}, "0"),
    "L": ("0.001", {"length": 3}, "0"),
    "mL": ("0.000001", {"length": 3}, "0"),
    "Hz": ("1", {"time": -1}, "0"),
    "rpm": ("0.10471975511965977461542144610931676280657234114716",
            {"time": -1}, "0"),  # 2*pi/60 rad/s; angle is dimensionless
    # Mechanics.
    "N": ("1", {"mass": 1, "length": 1, "time": -2}, "0"),
    "Pa": ("1", {"mass": 1, "length": -1, "time": -2}, "0"),
    "kPa": ("1000", {"mass": 1, "length": -1, "time": -2}, "0"),
    "bar": ("100000", {"mass": 1, "length": -1, "time": -2}, "0"),
    "J": ("1", {"mass": 1, "length": 2, "time": -2}, "0"),
    "W": ("1", {"mass": 1, "length": 2, "time": -3}, "0"),
    "kW": ("1000", {"mass": 1, "length": 2, "time": -3}, "0"),
    "MW": ("1000000", {"mass": 1, "length": 2, "time": -3}, "0"),
    "Wh": ("3600", {"mass": 1, "length": 2, "time": -2}, "0"),
    "kWh": ("3600000", {"mass": 1, "length": 2, "time": -2}, "0"),
    # Electromagnetism.
    "V": ("1", {"mass": 1, "length": 2, "time": -3, "current": -1}, "0"),
    "mV": ("0.001", {"mass": 1, "length": 2, "time": -3, "current": -1}, "0"),
    "kV": ("1000", {"mass": 1, "length": 2, "time": -3, "current": -1}, "0"),
    "mA": ("0.001", {"current": 1}, "0"),
    "Ohm": ("1", {"mass": 1, "length": 2, "time": -3, "current": -2}, "0"),
    "ohm": ("1", {"mass": 1, "length": 2, "time": -3, "current": -2}, "0"),
    "C": ("1", {"time": 1, "current": 1}, "0"),
    "F": ("1", {"mass": -1, "length": -2, "time": 4, "current": 2}, "0"),
    "H": ("1", {"mass": 1, "length": 2, "time": -2, "current": -2}, "0"),
    # Affine (offset) temperature scales.
    "degC": ("1", {"temperature": 1}, "273.15"),
    "degF": ("0.55555555555555555555555555555555555555555555555556",
             {"temperature": 1}, "255.37222222222222222222222222222222222222222222222"),
}

SI_PREFIXES: dict[str, str] = {
    "p": "0.000000000001",
    "n": "0.000000001",
    "u": "0.000001",
    "µ": "0.000001",
    "m": "0.001",
    "c": "0.01",
    "d": "0.1",
    "da": "10",
    "h": "100",
    "k": "1000",
    "M": "1000000",
    "G": "1000000000",
    "T": "1000000000000",
}

class UnitError(ValueError):
    """Raised when a unit string cannot be parsed or combined."""


def _dimension_tuple(dim: dict[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted((key, exponent) for key, exponent in dim.items() if exponent))


@dataclass(frozen=True)
class UnitSpec:
    """A parsed unit: canonical label, SI factor/offset and dimension map."""

    label: str
    factor: Decimal
    offset: Decimal
    dimension: tuple[tuple[str, int], ...]

    @property
    def is_dimensionless(self) -> bool:
        return not self.dimension

    @property
    def is_affine(self) -> bool:
        """Affine (offset) units cannot be multiplied without conversion."""
        return self.offset != Decimal(0)

    def to_si(self, value: Decimal) -> Decimal:
        return value * self.factor + self.offset

    def from_si(self, value_si: Decimal) -> Decimal:
        return (value_si - self.offset) / self.factor


_DIMENSIONLESS = UnitSpec("", Decimal(1), Decimal(0), ())


def _parse_atom(token: str) -> UnitSpec:
    if token == "1" or token == "":
        return _DIMENSIONLESS
    if token in _UNIT_TABLE:
        factor_text, dim, offset_text = _UNIT_TABLE[token]
        return UnitSpec(token, Decimal(factor_text), Decimal(offset_text),
                        _dimension_tuple(dim))
    # Generic prefixed form, e.g. mV, kPa — but never double-prefix a table unit.
    for prefix_length in (2, 1):
        if len(token) > prefix_length:
            prefix = token[:prefix_length]
            base = token[prefix_length:]
            if prefix in SI_PREFIXES and base in _UNIT_TABLE:
                factor_text, dim, offset_text = _UNIT_TABLE[base]
                if Decimal(offset_text) != 0:
                    raise UnitError(
                        f"unit {token!r}: SI prefixes are not allowed on offset "
                        f"unit {base!r}; convert to an absolute scale first"
                    )
                label = prefix + base
                return UnitSpec(label, Decimal(SI_PREFIXES[prefix]) * Decimal(factor_text),
                                Decimal(0), _dimension_tuple(dim))
    raise UnitError(f"unknown unit {token!r}")


_TERM_RE = re.compile(r"^([A-Za-zµ_][A-Za-z_]*)(?:\^?(-?\d+))?$|^(1)$")


def _parse_term(term: str) -> UnitSpec:
    match = _TERM_RE.match(term)
    if not match:
        raise UnitError(f"invalid unit term {term!r}")
    if match.group(3) is not None:
        return _DIMENSIONLESS
    atom = _parse_atom(match.group(1))
    exponent = int(match.group(2)) if match.group(2) else 1
    if exponent == 0:
        return _DIMENSIONLESS
    if atom.is_affine and exponent != 1:
        raise UnitError(
            f"offset unit {atom.label!r} cannot be raised to a power; convert to "
            "an absolute scale (e.g. K) first"
        )
    dim = {key: value * exponent for key, value in atom.dimension}
    return UnitSpec(
        f"{atom.label}^{exponent}" if exponent != 1 else atom.label,
        atom.factor ** exponent,
        atom.offset * exponent if exponent == 1 else Decimal(0),
        _dimension_tuple(dim),
    )


def parse_unit(text: str) -> UnitSpec:
    """Parse a unit string such as ``V``, ``m3/h``, ``kg*m/s^2`` or ``1``.

    Multiplication uses ``*`` (or adjacency inside an atom), division ``/``,
    integer powers ``^n``. An empty string means dimensionless.
    """
    if text is None or not text.strip():
        return _DIMENSIONLESS
    normalized = text.strip().replace("Ω", "Ohm").replace("³", "^3").replace("²", "^2")
    # Split into multiplicative terms while tracking division.
    compact = normalized.replace(" ", "")
    tokens = re.findall(r"[A-Za-zµ_][A-Za-z_]*(?:\^?-?\d+)?|1|[*/]", compact)
    if "".join(tokens) != compact:
        raise UnitError(f"invalid unit {text!r}")
    if len(tokens) == 1 and "/" not in compact and "*" not in compact:
        return _parse_term(tokens[0])
    result_dim: dict[str, int] = {}
    factor = Decimal(1)
    sign = 1
    expect_term = True
    label_parts: list[str] = []
    for token in tokens:
        if token in ("*", "/"):
            if expect_term:
                raise UnitError(f"invalid unit {text!r}")
            sign = 1 if token == "*" else -1
            expect_term = True
            continue
        term = _parse_term(token)
        if term.is_affine and len(tokens) > 1:
            raise UnitError(
                f"offset unit {term.label!r} cannot appear in a compound unit; "
                "convert to an absolute scale (e.g. K) first"
            )
        for key, exponent in term.dimension:
            result_dim[key] = result_dim.get(key, 0) + exponent * sign
        factor *= term.factor ** sign
        if sign > 0:
            label_parts.append(("*" if label_parts else "") + term.label)
        else:
            label_parts.append(f"/{term.label}")
        expect_term = False
    if expect_term:
        raise UnitError(f"invalid unit {text!r}")
    return UnitSpec(text.strip(), factor, Decimal(0), _dimension_tuple(result_dim))


def combine_dimensions(left: UnitSpec, right: UnitSpec, multiply: bool) -> tuple:
    """Return the resulting dimension tuple for ``left * right`` or ``/``."""
    dim: dict[str, int] = dict(left.dimension)
    for key, exponent in right.dimension:
        dim[key] = dim.get(key, 0) + (exponent if multiply else -exponent)
    return _dimension_tuple(dim)


def same_dimension(left: UnitSpec, right: UnitSpec) -> bool:
    return left.dimension == right.dimension


# --- Exact Decimal interval and scalar helpers ------------------------------


def to_decimal(value: int | float | str | Decimal) -> Decimal:
    """Convert an engineering float through its shortest exact round-trip text."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodecError("non_finite", "only finite values can be used in derivation")
        return Decimal(repr(value))
    return Decimal(str(value))


def decimal_to_float(value: Decimal) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise CodecError("non_finite", "derived value is not representable as a finite float")
    return result


def quantize_decimal(value: Decimal, places: int = 9) -> Decimal:
    """Stable rounding for storage/reporting without changing Decimal math."""
    quantum = Decimal(1).scaleb(-places)
    return value.quantize(quantum)


def sqrt_decimal(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return value.sqrt()


DATA_TYPES = ("int16", "uint16", "int32", "uint32", "float32")
BYTE_ORDERS_16 = ("AB", "BA")
BYTE_ORDERS_32 = ("ABCD", "CDAB", "BADC", "DCBA")
ALL_BYTE_ORDERS = BYTE_ORDERS_16 + BYTE_ORDERS_32
REGISTER_COUNTS = {
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
}
SIGNED_TYPES = {"int16", "int32"}
WIDTHS = {"int16": 2, "uint16": 2, "int32": 4, "uint32": 4, "float32": 4}


class CodecError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _wire_to_big_endian(data: bytes, byte_order: str) -> bytes:
    if len(data) == 2:
        if byte_order == "AB":
            return data
        if byte_order == "BA":
            return data[::-1]
    if len(data) == 4:
        if byte_order == "ABCD":
            return data
        if byte_order == "CDAB":
            return data[2:4] + data[0:2]
        if byte_order == "BADC":
            return bytes((data[1], data[0], data[3], data[2]))
        if byte_order == "DCBA":
            return data[::-1]
    raise CodecError(
        "unsupported_byte_order", f"{byte_order} is not valid for {len(data)} bytes"
    )


def _big_endian_to_wire(data: bytes, byte_order: str) -> bytes:
    # All supported byte-order transforms are their own inverse.
    return _wire_to_big_endian(data, byte_order)


def decode_value(data: bytes, data_type: str, byte_order: str) -> int | float:
    """Decode register bytes, independent of host endianness."""
    if data_type not in REGISTER_COUNTS:
        raise CodecError("unsupported_data_type", f"unsupported type {data_type}")
    expected = REGISTER_COUNTS[data_type] * 2
    if len(data) != expected:
        raise CodecError(
            "payload_length",
            f"{data_type} needs {expected} bytes, got {len(data)}",
        )
    allowed = BYTE_ORDERS_16 if expected == 2 else BYTE_ORDERS_32
    if byte_order not in allowed:
        raise CodecError(
            "unsupported_byte_order", f"{byte_order} is not valid for {data_type}"
        )
    normalized = _wire_to_big_endian(data, byte_order)
    if data_type == "float32":
        value = struct.unpack(">f", normalized)[0]
        if not math.isfinite(value):
            raise CodecError("non_finite", "decoded float is not finite")
        return value
    return int.from_bytes(normalized, "big", signed=data_type in SIGNED_TYPES)


def encode_raw(raw: int | float, data_type: str, byte_order: str) -> bytes:
    """Encode a raw value back to wire register bytes (used by fixtures/tests)."""
    if data_type not in REGISTER_COUNTS:
        raise CodecError("unsupported_data_type", f"unsupported type {data_type}")
    if data_type == "float32":
        if not math.isfinite(float(raw)):
            raise CodecError("non_finite", "float raw value is not finite")
        normalized = struct.pack(">f", float(raw))
    else:
        if isinstance(raw, float) and not raw.is_integer():
            raise CodecError("invalid_raw", "integer type requires an integer raw value")
        normalized = int(raw).to_bytes(
            WIDTHS[data_type], "big", signed=data_type in SIGNED_TYPES
        )
    return _big_endian_to_wire(normalized, byte_order)


def apply_engineering(raw: int | float, scale: float, offset: float) -> float:
    value = float(raw) * float(scale) + float(offset)
    if not math.isfinite(value):
        raise CodecError("non_finite", "engineering value is not finite")
    return value


def classify_range(value: float, minimum: float, maximum: float) -> str | None:
    if value > maximum:
        return "overflow"
    if value < minimum:
        return "under_range"
    return None


def enum_label(mapping: dict[int, str], raw: int | float) -> tuple[bool, str | None]:
    """Return (in_enum, label). Empty mappings are treated as unconstrained."""
    if not mapping:
        return True, None
    if isinstance(raw, float) and not raw.is_integer():
        return False, None
    key = int(raw)
    return key in mapping, mapping.get(key)
