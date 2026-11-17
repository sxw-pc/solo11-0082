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
class ModeStateContract:
    """Declared temporal semantics for one enum-mode point.

    ``transitions`` maps a source raw enum value to the set of raw enum values
    it may legally transition to. ``min_dwell_seconds`` gives the minimum time
    a value must remain current before it may be left again. ``max_gap_seconds``
    is the largest unobserved interval that may still be bridged by assuming an
    intermediate state; ``reset_state`` is the only value that may be entered
    through an explicitly declared edge without observing a predecessor.
    ``terminal_states`` explicitly marks states that are allowed to have no
    outgoing edge.
    """

    point_key: str
    transitions: dict[int, tuple[int, ...]] = field(default_factory=dict)
    min_dwell_seconds: dict[int, int] = field(default_factory=dict)
    max_gap_seconds: int = 0
    reset_state: int | None = None
    verification: str = "hop"
    terminal_states: tuple[int, ...] = ()

    @property
    def states(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.transitions) | {
            target for targets in self.transitions.values() for target in targets
        } | set(self.min_dwell_seconds) | set(self.terminal_states) | (
            {self.reset_state} if self.reset_state is not None else set()
        )))

    def dwell(self, state: int) -> int:
        return int(self.min_dwell_seconds.get(state, 0))

    def targets(self, state: int) -> tuple[int, ...]:
        return self.transitions.get(state, ())

    def has_edge(self, source: int, target: int) -> bool:
        return target in self.transitions.get(source, ())


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
    mode_state: ModeStateContract | None = None

    @property
    def match_key(self) -> tuple[int, int, int, int]:
        return (self.slave_id, self.function, self.start, self.count)

    @property
    def address_end(self) -> int:
        return self.start + self.count - 1


@dataclass(frozen=True)
class ContractSet:
    version: int
    devices: tuple[Device, ...]
    points: tuple[PointContract, ...]
    by_match: dict[tuple[int, int, int, int], PointContract]
    by_key: dict[str, PointContract]

    def find(self, slave_id: int, function: int, start: int, count: int):
        return self.by_match.get((slave_id, function, start, count))

    def get(self, point_key: str):
        return self.by_key.get(point_key)

    def device_label(self, device_id: str) -> str:
        for device in self.devices:
            if device.device_id == device_id:
                return device.label
        return device_id


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


def _parse_mode_state(
    raw: Any, location: str, enum: dict[int, str]
) -> ModeStateContract | None:
    """Validate a ``mode_state`` declaration before any batch is written.

    The graph references raw enum values only. Every referenced value must
    exist in the point's enum mapping, every non-terminal state must have at
    least one outgoing edge, and dwell requirements must be non-negative,
    free of unreachable entries and satisfiable along at least one declared
    edge into the state.
    """
    if raw is None:
        return None
    _require(isinstance(raw, dict), "contract_mode", "mode_state must be a mapping", location)
    _require(bool(enum), "contract_mode",
             "mode_state requires a non-empty enum mapping on the same point", location)

    def state_value(value: Any, what: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(
                "contract_mode", f"{what} must be an integer enum value", location
            )
        _require(value in enum, "contract_mode_enum",
                 f"{what} {value} is not declared in the point enum", location)
        return value

    verification = raw.get("verification", "hop")
    _require(verification in ("hop", "bounded"), "contract_mode",
             "verification must be 'hop' or 'bounded'", location)

    raw_graph = raw.get("transitions")
    _require(isinstance(raw_graph, dict) and bool(raw_graph), "contract_mode",
             "transitions must be a non-empty mapping", location)
    transitions: dict[int, tuple[int, ...]] = {}
    for raw_source, raw_targets in raw_graph.items():
        source = state_value(raw_source, "transition source")
        _require(source not in transitions, "contract_mode",
                 f"duplicate transition source {source}", location)
        _require(isinstance(raw_targets, list) and bool(raw_targets), "contract_mode",
                 f"transitions from {source} must be a non-empty list", location)
        targets: list[int] = []
        for raw_target in raw_targets:
            target = state_value(raw_target, "transition target")
            _require(target not in targets, "contract_mode",
                     f"duplicate transition {source}->{target}", location)
            targets.append(target)
        transitions[source] = tuple(targets)

    max_gap = raw.get("max_gap_seconds", 0)
    _require(isinstance(max_gap, int) and not isinstance(max_gap, bool) and max_gap >= 0,
             "contract_mode_gap", "max_gap_seconds must be a non-negative integer",
             location)
    reset_state = raw.get("reset_state")
    if reset_state is not None:
        reset_state = state_value(reset_state, "reset_state")
    raw_terminal = raw.get("terminal_states", [])
    _require(isinstance(raw_terminal, list), "contract_mode",
             "terminal_states must be a list", location)
    terminal: list[int] = []
    for raw_state in raw_terminal:
        state = state_value(raw_state, "terminal state")
        _require(state not in terminal, "contract_mode",
                 f"duplicate terminal state {state}", location)
        terminal.append(state)

    raw_dwell = raw.get("min_dwell_seconds", {})
    _require(isinstance(raw_dwell, dict), "contract_mode_dwell",
             "min_dwell_seconds must be a mapping", location)
    dwell: dict[int, int] = {}
    for raw_state, seconds in raw_dwell.items():
        state = state_value(raw_state, "dwell state")
        _require(isinstance(seconds, int) and not isinstance(seconds, bool) and seconds >= 0,
                 "contract_mode_dwell",
                 f"min_dwell_seconds for {state} must be a non-negative integer",
                 location)
        _require(state not in terminal, "contract_mode_dwell",
                 f"min_dwell_seconds names terminal state {state} that cannot be left",
                 location)
        dwell[state] = seconds

    mode = ModeStateContract(
        point_key="",
        transitions=transitions,
        min_dwell_seconds=dwell,
        max_gap_seconds=int(max_gap),
        reset_state=reset_state,
        verification=str(verification),
        terminal_states=tuple(terminal),
    )
    _validate_graph_semantics(mode, location)
    return mode


def _validate_graph_semantics(mode: ModeStateContract, location: str) -> None:
    check = GraphValidation(mode)
    errors = check.errors()
    if errors:
        code, detail = errors[0]
        raise ContractError(code, detail, location)


def check_mode_graph(
    transitions: dict[int, tuple[int, ...]],
    min_dwell_seconds: dict[int, int] | None = None,
    max_gap_seconds: int = 0,
    reset_state: int | None = None,
    verification: str = "hop",
    terminal_states: tuple[int, ...] = (),
) -> list[tuple[str, str]]:
    """Return every static problem in a mode graph as ``(code, detail)``."""
    mode = ModeStateContract(
        point_key="",
        transitions=dict(transitions),
        min_dwell_seconds=dict(min_dwell_seconds or {}),
        max_gap_seconds=int(max_gap_seconds),
        reset_state=reset_state,
        verification=verification,
        terminal_states=tuple(terminal_states),
    )
    return GraphValidation(mode).errors()


class GraphValidation:
    """Pure graph checks shared by contract parsing and direct API users."""

    def __init__(self, mode: ModeStateContract):
        self.mode = mode

    def errors(self) -> list[tuple[str, str]]:
        mode = self.mode
        errors: list[tuple[str, str]] = []
        sources = set(mode.transitions)
        targets = {target for targets in mode.transitions.values() for target in targets}
        terminal = set(mode.terminal_states)
        if not mode.transitions:
            return [("contract_mode", "transitions must be a non-empty mapping")]
        for source, targets_ in mode.transitions.items():
            if not targets_:
                errors.append(("contract_mode",
                               f"state {source} is non-terminal but has no legal exit"))
        for target in sorted(targets - sources - terminal):
            errors.append(("contract_mode_dead_end",
                           f"state {target} is reachable but has no outgoing transition "
                           "and is not declared terminal"))
        for state in sorted(terminal):
            if state in sources:
                errors.append(("contract_mode",
                               f"terminal state {state} also declares outgoing transitions"))
        for state in sorted(mode.min_dwell_seconds):
            if state not in sources:
                errors.append(("contract_mode_dwell",
                               f"min_dwell_seconds names unreachable state {state} "
                               "with no exits"))
        # A state with a positive dwell must itself be entered. If no declared
        # edge enters it, the requirement can never be satisfied.
        for state, seconds in sorted(mode.min_dwell_seconds.items()):
            if seconds > 0 and state not in targets and state != mode.reset_state:
                errors.append(("contract_mode_dwell",
                               f"min_dwell_seconds for {state} cannot be satisfied: "
                               "no transition enters it and it is not the reset state"))
        reset = mode.reset_state
        if reset is not None:
            if reset not in sources:
                errors.append(("contract_mode_reset",
                               f"reset_state {reset} has no declared outgoing transition"))
            if reset not in targets:
                errors.append(("contract_mode_reset",
                               f"reset_state {reset} is not reachable through any "
                               "declared edge; arbitrary jumps cannot be called a reset"))
        if mode.verification not in ("hop", "bounded"):
            errors.append(("contract_mode",
                           "verification must be 'hop' or 'bounded'"))
        if mode.max_gap_seconds < 0:
            errors.append(("contract_mode_gap",
                           "max_gap_seconds must be a non-negative integer"))
        # Global satisfiability: every positive-dwell state must lie on a legal
        # cycle, otherwise the dwell requirement cannot be met by any observed
        # sequence that leaves and re-enters it.
        if any(seconds > 0 for seconds in mode.min_dwell_seconds.values()):
            stuck = self._states_off_cycle()
            if stuck:
                errors.append(("contract_mode_dwell",
                               "min_dwell_seconds is unsatisfiable: no legal cycle "
                               f"returns to state(s) {sorted(stuck)}"))
        return errors

    def _states_off_cycle(self) -> set[int]:
        dwelled = {state for state, seconds in self.mode.min_dwell_seconds.items()
                   if seconds > 0}
        if not dwelled:
            return set()
        adjacency = {state: set(targets) for state, targets in self.mode.transitions.items()}
        stuck: set[int] = set()
        for state in dwelled:
            reachable: set[int] = set()
            frontier = list(adjacency.get(state, ()))
            while frontier:
                current = frontier.pop()
                if current in reachable:
                    continue
                reachable.add(current)
                frontier.extend(adjacency.get(current, ()))
            if state not in reachable:
                stuck.add(state)
        return stuck


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
    mode_state = _parse_mode_state(raw.get("mode_state"), location, enum)
    if mode_state is not None:
        mode_state = ModeStateContract(
            point_key=key,
            transitions=mode_state.transitions,
            min_dwell_seconds=mode_state.min_dwell_seconds,
            max_gap_seconds=mode_state.max_gap_seconds,
            reset_state=mode_state.reset_state,
            verification=mode_state.verification,
            terminal_states=mode_state.terminal_states,
        )
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
        mode_state=mode_state,
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
    return ContractSet(
        version=version,
        devices=tuple(devices),
        points=tuple(points),
        by_match={point.match_key: point for point in points},
        by_key={point.point_key: point for point in points},
    )


def load_contract(path: str | Path) -> ContractSet:
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError("contract_read", f"cannot read contract: {exc}") from exc
    return parse_contract(text, str(target))
