"""Declarative point contracts and strict pre-ingest validation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .codec import BYTE_ORDERS_16, BYTE_ORDERS_32, DATA_TYPES, REGISTER_COUNTS
from .util import parse_utc_timestamp

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

REPORT_VIEWS = ("as_recorded", "as_effective")


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
class CalibrationVersion:
    """One calibration version: coefficients plus non-overlapping UTC intervals.

    Intervals are half-open [start, end); an end of None means open-ended.
    """

    version_id: str
    scale: float
    offset: float
    intervals: tuple[tuple[datetime, datetime | None], ...]

    def covers(self, moment: datetime) -> bool:
        return any(
            start <= moment and (end is None or moment < end)
            for start, end in self.intervals
        )


@dataclass(frozen=True)
class CalibrationRevision:
    """One registered calibration sheet. Later registrations replace the
    effective-interval mapping of earlier ones; earlier rows remain evidence."""

    registered_at: datetime
    versions: tuple[CalibrationVersion, ...]

    def version_at(self, moment: datetime) -> CalibrationVersion | None:
        for version in self.versions:
            if version.covers(moment):
                return version
        return None


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
    calibrations: tuple[CalibrationRevision, ...] = ()

    @property
    def match_key(self) -> tuple[int, int, int, int]:
        return (self.slave_id, self.function, self.start, self.count)

    @property
    def address_end(self) -> int:
        return self.start + self.count - 1


def resolve_calibration(
    point: PointContract, moment: datetime
) -> tuple[CalibrationRevision, CalibrationVersion] | None:
    """Resolve the calibration effective at ``moment``.

    Only the latest registered revision is authoritative: it replaces the
    effective intervals of older revisions. Returns None when the point has
    no calibration table or the timestamp falls into a gap (undeterminable);
    callers must not guess from neighbouring versions.
    """
    if not point.calibrations:
        return None
    revision = point.calibrations[-1]
    version = revision.version_at(moment)
    if version is None:
        return None
    return revision, version


@dataclass(frozen=True)
class ContractSet:
    version: int
    devices: tuple[Device, ...]
    points: tuple[PointContract, ...]
    by_match: dict[tuple[int, int, int, int], PointContract]
    by_key: dict[str, PointContract]
    report_views: tuple[str, ...] = ("as_recorded",)

    def find(self, slave_id: int, function: int, start: int, count: int):
        return self.by_match.get((slave_id, function, start, count))

    def get(self, point_key: str):
        return self.by_key.get(point_key)

    def device_label(self, device_id: str) -> str:
        for device in self.devices:
            if device.device_id == device_id:
                return device.label
        return device_id

    def has_calibrations(self) -> bool:
        return any(point.calibrations for point in self.points)


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


def _to_utc(value: Any) -> datetime | None:
    """Accept a YAML timestamp (datetime) or an ISO string; require UTC-aware."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)
    return parse_utc_timestamp(value)


def _parse_intervals(raw: Any, location: str) -> tuple[tuple[datetime, datetime | None], ...]:
    _require(
        isinstance(raw, list) and bool(raw),
        "contract_calibration",
        "effective must be a non-empty list of [start, end] pairs",
        location,
    )
    intervals: list[tuple[datetime, datetime | None]] = []
    for item in raw:
        _require(
            isinstance(item, (list, tuple)) and len(item) == 2,
            "contract_calibration",
            "effective interval must be a [start, end] pair",
            location,
        )
        start = _to_utc(item[0])
        _require(
            start is not None,
            "contract_calibration",
            f"interval start {item[0]!r} is not an aware UTC timestamp",
            location,
        )
        end: datetime | None = None
        if item[1] is not None:
            end = _to_utc(item[1])
            _require(
                end is not None,
                "contract_calibration",
                f"interval end {item[1]!r} is not an aware UTC timestamp",
                location,
            )
            _require(
                start < end,
                "contract_calibration",
                "interval start must be before interval end",
                location,
            )
        intervals.append((start, end))
    return tuple(intervals)


def _check_no_overlap(
    entries: list[tuple[str, datetime, datetime | None]], location: str
) -> None:
    """Reject overlapping effective intervals inside one revision.

    Intervals are half-open, so touching bounds (end == next start) are fine.
    Gaps are allowed here; they surface as undeterminable readings at runtime.
    """
    ordered = sorted(entries, key=lambda entry: (entry[1], entry[0]))
    max_end: datetime | None = None
    max_end_id = ""
    open_ended_id: str | None = None
    for version_id, start, end in ordered:
        if open_ended_id is not None:
            raise ContractError(
                "contract_calibration_overlap",
                f"effective interval of version {version_id!r} overlaps "
                f"open-ended interval of version {open_ended_id!r}",
                location,
            )
        if max_end is not None and start < max_end:
            raise ContractError(
                "contract_calibration_overlap",
                f"effective interval of version {version_id!r} overlaps "
                f"version {max_end_id!r}",
                location,
            )
        if end is None:
            open_ended_id = version_id
        elif max_end is None or end > max_end:
            max_end = end
            max_end_id = version_id


def _parse_calibrations(raw: Any, location: str) -> tuple[CalibrationRevision, ...]:
    if raw is None:
        return ()
    _require(
        isinstance(raw, list) and bool(raw),
        "contract_calibration",
        "calibrations must be a non-empty list of revisions",
        location,
    )
    revisions: list[CalibrationRevision] = []
    seen_registered: set[datetime] = set()
    seen_version_ids: set[str] = set()
    for index, raw_revision in enumerate(raw, start=1):
        rev_location = f"{location}.calibrations[{index}]"
        _require(
            isinstance(raw_revision, dict),
            "contract_calibration",
            "calibration revision must be a mapping",
            rev_location,
        )
        registered = _to_utc(raw_revision.get("registered_at"))
        _require(
            registered is not None,
            "contract_calibration",
            "registered_at must be an aware UTC ISO 8601 timestamp",
            rev_location,
        )
        _require(
            registered not in seen_registered,
            "contract_calibration",
            "two revisions of one point share the same registered_at",
            rev_location,
        )
        seen_registered.add(registered)
        raw_versions = raw_revision.get("versions")
        _require(
            isinstance(raw_versions, list) and bool(raw_versions),
            "contract_calibration",
            "versions must be a non-empty list",
            rev_location,
        )
        versions: list[CalibrationVersion] = []
        interval_entries: list[tuple[str, datetime, datetime | None]] = []
        for version_index, raw_version in enumerate(raw_versions, start=1):
            version_location = f"{rev_location}.versions[{version_index}]"
            _require(
                isinstance(raw_version, dict),
                "contract_calibration",
                "calibration version must be a mapping",
                version_location,
            )
            version_id = raw_version.get("version_id")
            _require(
                isinstance(version_id, str) and bool(KEY_RE.match(version_id)),
                "contract_calibration",
                "version_id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}",
                version_location,
            )
            _require(
                version_id not in seen_version_ids,
                "contract_calibration",
                f"duplicate calibration version id {version_id}",
                version_location,
            )
            seen_version_ids.add(version_id)
            scale = raw_version.get("scale", 1)
            _require(
                _number(scale) and float(scale) != 0.0,
                "contract_scale",
                "calibration scale must be finite and non-zero",
                version_location,
            )
            offset = raw_version.get("offset", 0)
            _require(
                _number(offset),
                "contract_offset",
                "calibration offset must be finite",
                version_location,
            )
            intervals = _parse_intervals(raw_version.get("effective"), version_location)
            for start, end in intervals:
                interval_entries.append((version_id, start, end))
            versions.append(
                CalibrationVersion(version_id, float(scale), float(offset), intervals)
            )
        _check_no_overlap(interval_entries, rev_location)
        revisions.append(CalibrationRevision(registered, tuple(versions)))
    revisions.sort(key=lambda revision: revision.registered_at)
    return tuple(revisions)


def _parse_report_views(raw: Any, has_calibrations: bool, source: str) -> tuple[str, ...]:
    if raw is None:
        return ("as_recorded", "as_effective") if has_calibrations else ("as_recorded",)
    location = f"{source}:report"
    _require(isinstance(raw, dict), "contract_report", "report must be a mapping", location)
    views = raw.get("views")
    _require(
        isinstance(views, list) and bool(views),
        "contract_report",
        "report.views must be a non-empty list",
        location,
    )
    result: list[str] = []
    for view in views:
        _require(
            view in REPORT_VIEWS,
            "contract_report",
            f"unknown report view {view!r}; expected one of {', '.join(REPORT_VIEWS)}",
            location,
        )
        _require(
            view not in result,
            "contract_report",
            f"duplicate report view {view!r}",
            location,
        )
        result.append(view)
    return tuple(result)


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
    calibrations = _parse_calibrations(raw.get("calibrations"), location)
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
        calibrations=calibrations,
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
    report_views = _parse_report_views(
        root.get("report"), any(point.calibrations for point in points), source
    )
    return ContractSet(
        version=version,
        devices=tuple(devices),
        points=tuple(points),
        by_match={point.match_key: point for point in points},
        by_key={point.point_key: point for point in points},
        report_views=report_views,
    )


def load_contract(path: str | Path) -> ContractSet:
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError("contract_read", f"cannot read contract: {exc}") from exc
    return parse_contract(text, str(target))
