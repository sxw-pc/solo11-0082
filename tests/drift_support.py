"""Shared synthetic-replay helpers for the drift-review tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pointlog.codec import encode_raw
from pointlog.contract import ContractSet, load_contract
from pointlog.modbus import build_request, build_response, corrupt_crc
from pointlog.pipeline import run_capture

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "config" / "points.yaml"
POINT_KEY = "line_a_temperature"
SOURCE = "drift-lab"


def load_demo_contract() -> ContractSet:
    return load_contract(CONTRACT_PATH)


def stamp(day: int = 1, hour: int = 0, minute: int = 0, second: int = 0) -> str:
    return (
        datetime(2025, 4, day, hour, minute, second, tzinfo=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def make_record(
    contract: ContractSet,
    value: float,
    timestamp: str,
    *,
    point_key: str = POINT_KEY,
    corrupt_request_crc: bool = False,
    corrupt_response_crc: bool = False,
    empty_response: bool = False,
) -> dict[str, str]:
    point = contract.get(point_key)
    if point is None:
        raise KeyError(point_key)
    raw = round((float(value) - point.offset) / point.scale)
    payload = encode_raw(raw, point.data_type, point.byte_order)
    request = build_request(point.slave_id, point.function, point.start, point.count)
    response = build_response(point.slave_id, point.function, payload)
    if corrupt_request_crc:
        request = corrupt_crc(request)
    if corrupt_response_crc:
        response = corrupt_crc(response)
    return {
        "ts": timestamp,
        "request_hex": request.hex(),
        "response_hex": "" if empty_response else response.hex(),
        "source": SOURCE,
    }


def write_replay(path: Path, records: Iterable[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def training_records(
    contract: ContractSet,
    *,
    hours: Iterable[int] = range(24),
    day: int = 1,
    noon_hour: int = 12,
    normal: float = 20.0,
    noon_value: float = 30.0,
    jitter: tuple[float, ...] = (-1.0, 0.0, 1.0, 0.0, -1.0),
    step_minutes: int = 10,
) -> list[dict[str, str]]:
    """One hour of stationary samples per listed UTC hour, ±1 jitter.

    The target hour (default 12:00) sits around ``noon_value``; every other
    hour sits around ``normal``.  The jitter makes each layer's MAD non-zero.
    """
    records: list[dict[str, str]] = []
    for hour in hours:
        base = noon_value if hour == noon_hour else normal
        for index, offset in enumerate(jitter):
            records.append(make_record(
                contract,
                base + offset,
                stamp(day=day, hour=hour, minute=index * step_minutes),
            ))
    return records


def capture(
    db: Path,
    replay: Path,
    *,
    contract_path: Path = CONTRACT_PATH,
    stop_after: int | None = None,
) -> Any:
    return run_capture(
        replay_path=replay,
        contract_path=contract_path,
        db_path=db,
        reports_dir=None,
        stop_after=stop_after,
    )


def point_result(report: dict[str, Any], point_key: str = POINT_KEY) -> dict[str, Any]:
    for point in report["points"]:
        if point["point_key"] == point_key:
            return point
    raise KeyError(point_key)


def event_signature(report: dict[str, Any], point_key: str = POINT_KEY) -> list[tuple]:
    point = point_result(report, point_key)
    return [
        (
            event["direction"],
            event["start"]["ts"],
            event["detected_at"]["ts"],
            event["magnitude"],
            event["reference"],
        )
        for event in point["drifts"]
    ]
