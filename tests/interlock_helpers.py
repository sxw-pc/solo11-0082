"""Shared builders for interlock tests.

Everything here is synthetic and offline: frames are built with the same
deterministic encoder the demo fixtures use, then written to a JSONL replay.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import yaml

from pointlog.codec import encode_raw
from pointlog.contract import load_contract
from pointlog.modbus import build_request, build_response, corrupt_crc
from pointlog.pipeline import run_capture

BASE = datetime(2025, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
SOURCE = "interlock-fixture"


def ts(minutes: float = 0, seconds: float = 0) -> str:
    moment = BASE + timedelta(minutes=minutes, seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


def point(contract, key):
    found = contract.get(key)
    assert found is not None, key
    return found


def frame(contract, key, raw, when):
    definition = point(contract, key)
    payload = encode_raw(raw, definition.data_type, definition.byte_order)
    return {
        "ts": when,
        "request_hex": build_request(
            definition.slave_id, definition.function,
            definition.start, definition.count,
        ).hex(),
        "response_hex": build_response(
            definition.slave_id, definition.function, payload
        ).hex(),
        "source": SOURCE,
    }


def bad_crc_frame(contract, key, raw, when):
    record = frame(contract, key, raw, when)
    record["response_hex"] = corrupt_crc(
        bytes.fromhex(record["response_hex"])
    ).hex()
    return record


def contradictory_frame(contract, key, raw, when):
    """Same timestamp as a good sample but a different engineering value."""
    return frame(contract, key, raw, when)


def write_replay(path, records):
    text = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    path.write_text(text, encoding="utf-8")
    return path


def contract_yaml(points, interlocks, device_id="plant-a", labels=None):
    raw = {
        "version": 1,
        "devices": [{
            "id": device_id,
            "label": (labels or {}).get(device_id, "Plant A synthetic"),
            "points": points,
        }],
    }
    if interlocks:
        raw["devices"][0]["interlocks"] = interlocks
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)


TEMP_POINT = {
    "key": "temp",
    "slave_id": 1,
    "function": 3,
    "start": 0,
    "count": 1,
    "data_type": "int16",
    "byte_order": "AB",
    "scale": 0.1,
    "offset": 0,
    "unit": "degC",
    "range": [-40, 150],
}

PRESSURE_POINT = {
    "key": "pressure",
    "slave_id": 1,
    "function": 3,
    "start": 1,
    "count": 1,
    "data_type": "uint16",
    "byte_order": "AB",
    "scale": 0.1,
    "offset": 0,
    "unit": "kPa",
    "range": [0, 1000],
}


def interlock(
    *,
    sampling="bounded_hold",
    min_duration_minutes=2,
    max_stale_minutes=5,
    version=7,
    rule_id="hp",
    a_threshold=60,
    b_threshold=800,
):
    return {
        "id": rule_id,
        "version": version,
        "points": [
            {"key": "temp", "threshold": a_threshold, "direction": "above"},
            {"key": "pressure", "threshold": b_threshold, "direction": "above"},
        ],
        "min_duration_minutes": min_duration_minutes,
        "max_stale_minutes": max_stale_minutes,
        "sampling": sampling,
    }


def build_contract(
    tmp_path, *, sampling="bounded_hold", min_duration_minutes=2,
    max_stale_minutes=5, version=7, rule_id="hp",
    points=None, interlocks=None, name="contract.yaml",
):
    text = contract_yaml(
        points if points is not None else [TEMP_POINT, PRESSURE_POINT],
        interlocks if interlocks is not None else [
            interlock(
                sampling=sampling,
                min_duration_minutes=min_duration_minutes,
                max_stale_minutes=max_stale_minutes,
                version=version,
                rule_id=rule_id,
            )
        ],
    )
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path, load_contract(path)


def run(tmp_path, contract_path, records, *, resume=False, stop_after=None,
        db_name="ledger.db", fixture_name="replay.jsonl", reports_dir=None):
    fixture = write_replay(tmp_path / fixture_name, records)
    return run_capture(
        replay_path=fixture,
        contract_path=contract_path,
        db_path=tmp_path / db_name,
        reports_dir=None if reports_dir is None else tmp_path / reports_dir,
        resume=resume,
        stop_after=stop_after,
    )


# Canonical episode from the specification: temperature crosses high at
# 00:00 and returns to normal at 00:04; pressure crosses high at 00:02 and
# returns to normal at 00:06.  Samples are not simultaneous.
def canonical_episode(contract):
    return [
        frame(contract, "temp", 700, ts(0)),        # 70.0 degC high
        frame(contract, "pressure", 9000, ts(2)),   # 900.0 kPa high
        frame(contract, "temp", 500, ts(4)),        # 50.0 degC normal
        frame(contract, "pressure", 7000, ts(6)),   # 700.0 kPa normal
    ]
