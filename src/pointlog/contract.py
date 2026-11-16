"""Declarative point contracts and strict pre-ingest validation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .codec import BYTE_ORDERS_16, BYTE_ORDERS_32, DATA_TYPES, REGISTER_COUNTS

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

SAMPLING_BOUNDED_HOLD = "bounded_hold"
SAMPLING_STRICT_SIMULTANEOUS = "strict_simultaneous"
SAMPLING_MODES = (SAMPLING_BOUNDED_HOLD, SAMPLING_STRICT_SIMULTANEOUS)
SAMPLING_ALIASES = {
    SAMPLING_BOUNDED_HOLD: SAMPLING_BOUNDED_HOLD,
    "bounded": SAMPLING_BOUNDED_HOLD,
    "hold": SAMPLING_BOUNDED_HOLD,
    SAMPLING_STRICT_SIMULTANEOUS: SAMPLING_STRICT_SIMULTANEOUS,
    "strict": SAMPLING_STRICT_SIMULTANEOUS,
    "same_timestamp": SAMPLING_STRICT_SIMULTANEOUS,
    "simultaneous": SAMPLING_STRICT_SIMULTANEOUS,
}

DIRECTION_ABOVE = "above"
DIRECTION_BELOW = "below"
DIRECTIONS = (DIRECTION_ABOVE, DIRECTION_BELOW)
DIRECTION_ALIASES = {
    DIRECTION_ABOVE: DIRECTION_ABOVE,
    "high": DIRECTION_ABOVE,
    "greater": DIRECTION_ABOVE,
    "gt": DIRECTION_ABOVE,
    ">": DIRECTION_ABOVE,
    ">=": DIRECTION_ABOVE,
    DIRECTION_BELOW: DIRECTION_BELOW,
    "low": DIRECTION_BELOW,
    "less": DIRECTION_BELOW,
    "lt": DIRECTION_BELOW,
    "<": DIRECTION_BELOW,
    "<=": DIRECTION_BELOW,
}


class ContractError(ValueError):
    """A static contract error; raised before any batch is written."""

    def __init__(self, code: str, detail: str, location: str = ""):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.location = location

    def __str__(self) -> str:
        prefix = f"{self.location}: " if self.location else ""
        return f"[{self.code}] {prefix}{self.detail}"


@dataclass(frozen=True)
class Device:
    device_id: str
    label: str


@dataclass(frozen=True)
class PointContract:
    device_id: str
    point_key: str
    slave_id: int
    function: int
    start: int
    count: int
    data_type: str
    byte_order: str
    scale: float
    offset: float
    unit: str
    range_min: float
    range_max: float
    enum: dict[int, str] = field(default_factory=dict)
    description: str = ""

    @property
    def match_key(self) -> tuple[int, int, int, int]:
        return (self.slave_id, self.function, self.start, self.count)

    @property
    def address_end(self) -> int:
        return self.start + self.count - 1


@dataclass(frozen=True)
class InterlockPoint:
    """One side of a two-point sustained interlock rule."""

    point_key: str
    threshold: float
    direction: str = DIRECTION_ABOVE
    inclusive: bool = False

    def is_triggered(self, value: float) -> bool:
        if self.direction == DIRECTION_ABOVE:
            return value >= self.threshold if self.inclusive else value > self.threshold
        return value <= self.threshold if self.inclusive else value < self.threshold

    def describe(self) -> str:
        operator = self.direction
        if self.direction == DIRECTION_ABOVE:
            operator = ">=" if self.inclusive else ">"
        else:
            operator = "<=" if self.inclusive else "<"
        return f"{self.point_key} {operator} {self.threshold:g}"


@dataclass(frozen=True)
class InterlockRule:
    """A per-device rule combining two point conditions over time."""

    rule_id: str
    device_id: str
    version: int
    points: tuple[InterlockPoint, InterlockPoint]
    min_duration_minutes: float
    max_stale_minutes: float
    sampling: str = SAMPLING_BOUNDED_HOLD
    description: str = ""

    @property
    def min_duration_seconds(self) -> float:
        return self.min_duration_minutes * 60.0

    @property
    def max_stale_seconds(self) -> float:
        return self.max_stale_minutes * 60.0

    @property
    def point_keys(self) -> tuple[str, str]:
        return (self.points[0].point_key, self.points[1].point_key)

    @property
    def version_label(self) -> str:
        return f"{self.rule_id}@v{self.version}"


@dataclass(frozen=True)
class ContractSet:
    version: int
    devices: tuple[Device, ...]
    points: tuple[PointContract, ...]
    by_match: dict[tuple[int, int, int, int], PointContract]
    by_key: dict[str, PointContract]
    interlocks: tuple[InterlockRule, ...] = ()

    def find(self, slave_id: int, function: int, start: int, count: int):
        return self.by_match.get((slave_id, function, start, count))

    def get(self, point_key: str):
        return self.by_key.get(point_key)

    def device_label(self, device_id: str) -> str:
        for device in self.devices:
            if device.device_id == device_id:
                return device.label
        return device_id

    def interlocks_for(self, device_id: str) -> list[InterlockRule]:
        return [rule for rule in self.interlocks if rule.device_id == device_id]

    def interlock(self, rule_id: str) -> InterlockRule | None:
        for rule in self.interlocks:
            if rule.rule_id == rule_id:
                return rule
        return None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _require(condition: bool, code: str, detail: str, location: str = "") -> None:
    if not condition:
        raise ContractError(code, detail, location)


def _parse_enum(raw_enum: Any, location: str, data_type: str) -> dict[int, str]:
    if raw_enum is None:
        return {}
    _require(isinstance(raw_enum, dict), "contract_enum", "enum must be a mapping", location)
    if data_type == "float32" and raw_enum:
        raise ContractError("contract_enum", "float points cannot declare enum", location)
    result: dict[int, str] = {}
    for raw_key, raw_label in raw_enum.items():
        try:
            key = int(raw_key)
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "contract_enum", f"enum key {raw_key!r} is not an integer", location
            ) from exc
        if key in result:
            raise ContractError("contract_enum", f"duplicate enum key {key}", location)
        _require(
            isinstance(raw_label, str) and raw_label.strip() != "",
            "contract_enum",
            f"enum label for {key} must be a non-empty string",
            location,
        )
        result[key] = raw_label
    return result


def _parse_point(raw: Any, device_id: str, location: str) -> PointContract:
    _require(isinstance(raw, dict), "contract_point", "point must be a mapping", location)
    key = raw.get("key")
    _require(isinstance(key, str) and bool(KEY_RE.match(key)), "contract_point",
             "point key must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}", location)
    slave_id = raw.get("slave_id")
    _require(isinstance(slave_id, int) and not isinstance(slave_id, bool)
             and 1 <= slave_id <= 247, "contract_slave",
             "slave_id must be an integer in 1..247", location)
    function = raw.get("function")
    _require(function in (3, 4), "contract_function",
             "function must be 3 or 4", location)
    start = raw.get("start")
    _require(isinstance(start, int) and not isinstance(start, bool)
             and 0 <= start <= 65535, "contract_address",
             "start must be an integer in 0..65535", location)
    count = raw.get("count")
    _require(isinstance(count, int) and not isinstance(count, bool) and count > 0,
             "contract_register_count", "count must be a positive integer", location)
    _require(start + count - 1 <= 65535, "contract_address",
             "register range exceeds 65535", location)
    data_type = raw.get("data_type")
    _require(data_type in DATA_TYPES, "contract_data_type",
             f"data_type must be one of {', '.join(DATA_TYPES)}", location)
    _require(count == REGISTER_COUNTS[data_type], "contract_register_count",
             f"{data_type} requires exactly {REGISTER_COUNTS[data_type]} register(s)",
             location)
    byte_order = raw.get("byte_order")
    allowed = BYTE_ORDERS_16 if count == 1 else BYTE_ORDERS_32
    _require(byte_order in allowed, "contract_byte_order",
             f"byte_order must be one of {', '.join(allowed)}", location)
    scale = raw.get("scale", 1)
    _require(_number(scale) and float(scale) != 0.0, "contract_scale",
             "scale must be finite and non-zero", location)
    offset = raw.get("offset", 0)
    _require(_number(offset), "contract_offset", "offset must be finite", location)
    raw_range = raw.get("range")
    _require(isinstance(raw_range, (list, tuple)) and len(raw_range) == 2
             and _number(raw_range[0]) and _number(raw_range[1]),
             "contract_range", "range must be [min, max] finite numbers", location)
    range_min, range_max = float(raw_range[0]), float(raw_range[1])
    _require(range_min <= range_max, "contract_range",
             "range minimum must not exceed maximum", location)
    unit = raw.get("unit", "")
    _require(isinstance(unit, str), "contract_point", "unit must be a string", location)
    description = raw.get("description", "")
    _require(isinstance(description, str), "contract_point",
             "description must be a string", location)
    enum = _parse_enum(raw.get("enum"), location, data_type)
    return PointContract(
        device_id=device_id,
        point_key=key,
        slave_id=slave_id,
        function=function,
        start=start,
        count=count,
        data_type=data_type,
        byte_order=byte_order,
        scale=float(scale),
        offset=float(offset),
        unit=unit,
        range_min=range_min,
        range_max=range_max,
        enum=enum,
        description=description,
    )


def _parse_interlock_point(raw: Any, location: str) -> InterlockPoint:
    _require(isinstance(raw, dict), "contract_interlock",
             "interlock point must be a mapping", location)
    key = raw.get("key")
    _require(isinstance(key, str) and bool(KEY_RE.match(key)), "contract_interlock",
             "interlock point key is invalid", location)
    threshold = raw.get("threshold")
    _require(_number(threshold), "contract_interlock_threshold",
             "interlock threshold must be a finite number", location)
    raw_direction = raw.get("direction", DIRECTION_ABOVE)
    _require(isinstance(raw_direction, str), "contract_interlock_direction",
             "interlock direction must be a string", location)
    direction = DIRECTION_ALIASES.get(raw_direction.strip().lower())
    _require(direction is not None, "contract_interlock_direction",
             f"direction must be one of {', '.join(DIRECTIONS)}", location)
    raw_inclusive = raw.get("inclusive", raw_direction.strip() in (">=", "<="))
    _require(isinstance(raw_inclusive, bool), "contract_interlock_direction",
             "inclusive must be a boolean", location)
    return InterlockPoint(
        point_key=key,
        threshold=float(threshold),
        direction=direction,
        inclusive=raw_inclusive,
    )


def _parse_interlock(
    raw: Any,
    device_id: str,
    location: str,
    points_by_key: dict[str, PointContract],
) -> InterlockRule:
    _require(isinstance(raw, dict), "contract_interlock",
             "interlock must be a mapping", location)
    rule_id = raw.get("id")
    _require(isinstance(rule_id, str) and bool(KEY_RE.match(rule_id)),
             "contract_interlock", "interlock id is invalid", location)
    version = raw.get("version", 1)
    _require(isinstance(version, int) and not isinstance(version, bool) and version >= 1,
             "contract_interlock", "interlock version must be a positive integer",
             location)
    raw_points = raw.get("points")
    _require(isinstance(raw_points, list) and len(raw_points) == 2,
             "contract_interlock", "interlock must declare exactly two points", location)
    parsed = tuple(
        _parse_interlock_point(item, f"{location}.points[{index}]")
        for index, item in enumerate(raw_points, start=1)
    )
    _require(parsed[0].point_key != parsed[1].point_key, "contract_interlock_point",
             "interlock points must be two distinct point keys", location)
    for item in parsed:
        point = points_by_key.get(item.point_key)
        _require(point is not None, "contract_interlock_point",
                 f"interlock references unknown point {item.point_key}", location)
        _require(point.device_id == device_id, "contract_interlock_point",
                 f"interlock point {item.point_key} belongs to device "
                 f"{point.device_id}, not {device_id}", location)
    raw_duration = raw.get("min_duration_minutes")
    _require(_number(raw_duration) and float(raw_duration) > 0,
             "contract_interlock_duration",
             "min_duration_minutes must be a positive finite number", location)
    raw_stale = raw.get("max_stale_minutes")
    _require(_number(raw_stale) and float(raw_stale) > 0,
             "contract_interlock_stale",
             "max_stale_minutes must be a positive finite number", location)
    raw_sampling = raw.get("sampling", SAMPLING_BOUNDED_HOLD)
    _require(isinstance(raw_sampling, str), "contract_interlock_sampling",
             "sampling must be a string", location)
    sampling = SAMPLING_ALIASES.get(raw_sampling.strip().lower())
    _require(sampling is not None, "contract_interlock_sampling",
             f"sampling must be one of {', '.join(SAMPLING_MODES)}", location)
    description = raw.get("description", "")
    _require(isinstance(description, str), "contract_interlock",
             "interlock description must be a string", location)
    return InterlockRule(
        rule_id=rule_id,
        device_id=device_id,
        version=int(version),
        points=parsed,
        min_duration_minutes=float(raw_duration),
        max_stale_minutes=float(raw_stale),
        sampling=sampling,
        description=description,
    )


def parse_contract(text: str, source: str = "<memory>") -> ContractSet:
    try:
        root = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractError("contract_yaml", f"invalid YAML: {exc}") from exc
    _require(isinstance(root, dict), "contract_root", "contract root must be a mapping")
    version = root.get("version")
    _require(version == 1, "contract_version", "version must be 1")
    raw_devices = root.get("devices")
    _require(isinstance(raw_devices, list) and bool(raw_devices), "contract_devices",
             "devices must be a non-empty list")
    devices: list[Device] = []
    points: list[PointContract] = []
    raw_interlocks: list[tuple[str, str, Any]] = []
    seen_device_ids: set[str] = set()
    seen_keys: set[str] = set()
    seen_matches: set[tuple[int, int, int, int]] = set()
    for index, raw_device in enumerate(raw_devices, start=1):
        location = f"{source}:devices[{index}]"
        _require(isinstance(raw_device, dict), "contract_device",
                 "device must be a mapping", location)
        device_id = raw_device.get("id")
        _require(isinstance(device_id, str) and bool(ID_RE.match(device_id)),
                 "contract_device", "device id is invalid", location)
        _require(device_id not in seen_device_ids, "contract_device",
                 f"duplicate device id {device_id}", location)
        seen_device_ids.add(device_id)
        label = raw_device.get("label", device_id)
        _require(isinstance(label, str) and label.strip() != "", "contract_device",
                 "device label must be a non-empty string", location)
        devices.append(Device(device_id, label))
        raw_points = raw_device.get("points")
        _require(isinstance(raw_points, list) and bool(raw_points), "contract_point",
                 f"device {device_id} must declare at least one point", location)
        for point_index, raw_point in enumerate(raw_points, start=1):
            point_location = f"{location}.points[{point_index}]"
            point = _parse_point(raw_point, device_id, point_location)
            _require(point.point_key not in seen_keys, "contract_duplicate_key",
                     f"duplicate point key {point.point_key}", point_location)
            _require(point.match_key not in seen_matches, "contract_duplicate_match",
                     "two points use the same slave/function/start/count", point_location)
            seen_keys.add(point.point_key)
            seen_matches.add(point.match_key)
            points.append(point)
        raw_device_interlocks = raw_device.get("interlocks", [])
        _require(isinstance(raw_device_interlocks, list), "contract_interlock",
                 "interlocks must be a list", location)
        for interlock_index, raw_interlock in enumerate(raw_device_interlocks, start=1):
            raw_interlocks.append((
                device_id,
                f"{location}.interlocks[{interlock_index}]",
                raw_interlock,
            ))
    points_by_key = {point.point_key: point for point in points}
    interlocks: list[InterlockRule] = []
    seen_rule_ids: set[str] = set()
    for device_id, interlock_location, raw_interlock in raw_interlocks:
        rule = _parse_interlock(
            raw_interlock, device_id, interlock_location, points_by_key
        )
        _require(rule.rule_id not in seen_rule_ids, "contract_duplicate_interlock",
                 f"duplicate interlock id {rule.rule_id}", interlock_location)
        seen_rule_ids.add(rule.rule_id)
        interlocks.append(rule)
    return ContractSet(
        version=version,
        devices=tuple(devices),
        points=tuple(points),
        by_match={point.match_key: point for point in points},
        by_key=points_by_key,
        interlocks=tuple(interlocks),
    )


def load_contract(path: str | Path) -> ContractSet:
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError("contract_read", f"cannot read contract: {exc}") from exc
    return parse_contract(text, str(target))
