"""Deterministic synthetic replay-fixture generator for the demo."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .codec import encode_raw
from .contract import ContractSet, load_contract
from .modbus import (
    build_exception_response,
    build_request,
    build_response,
    corrupt_crc,
)
from .util import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_FIXTURE_PATH,
    atomic_write_text,
    sha256_text,
)

BASE_TIME = datetime(2025, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
STEP_SECONDS = 5
SOURCE = "demo-gateway"


def _ts(index: int) -> str:
    return (BASE_TIME + timedelta(seconds=STEP_SECONDS * index)).isoformat().replace(
        "+00:00", "Z"
    )


def _point(contract: ContractSet, key: str):
    point = contract.get(key)
    if point is None:
        raise ValueError(f"fixture references unknown contract point: {key}")
    return point


def _request_for(point) -> str:
    return build_request(point.slave_id, point.function, point.start, point.count).hex()


def _normal(contract: ContractSet, key: str, raw: int | float, timestamp: str) -> dict[str, str]:
    point = _point(contract, key)
    payload = encode_raw(raw, point.data_type, point.byte_order)
    response = build_response(point.slave_id, point.function, payload)
    return {
        "ts": timestamp,
        "request_hex": _request_for(point),
        "response_hex": response.hex(),
        "source": SOURCE,
    }


def _with_response(record: dict[str, str], response_hex: str) -> dict[str, str]:
    updated = dict(record)
    updated["response_hex"] = response_hex
    return updated


def build_demo_records(contract: ContractSet) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    counter = 0

    def add(record: dict[str, str]) -> None:
        nonlocal counter
        record = dict(record)
        record["ts"] = _ts(counter) if "ts" not in record else record["ts"]
        records.append(record)
        counter += 1

    # Eight clean reads across both synthetic devices.
    add(_normal(contract, "line_a_speed", 12340, _ts(counter)))
    add(_normal(contract, "line_a_temperature", 253, _ts(counter)))
    add(_normal(contract, "line_a_mode", 1, _ts(counter)))
    add(_normal(contract, "line_a_flow", 42.5, _ts(counter)))
    add(_normal(contract, "line_a_level", 225, _ts(counter)))
    add(_normal(contract, "line_a_voltage", 380500, _ts(counter)))
    add(_normal(contract, "line_a_pressure", 10130, _ts(counter)))
    add(_normal(contract, "line_a_status", 0, _ts(counter)))

    # Value-quality cases.
    add(_normal(contract, "line_a_speed", 50000, _ts(counter)))
    add(_normal(contract, "line_a_temperature", -600, _ts(counter)))
    add(_normal(contract, "line_a_mode", 7, _ts(counter)))

    # Exception response.
    flow = _point(contract, "line_a_flow")
    exception_record = {
        "ts": _ts(counter),
        "request_hex": _request_for(flow),
        "response_hex": build_exception_response(
            flow.slave_id, flow.function, 2
        ).hex(),
        "source": SOURCE,
    }
    add(exception_record)

    # Corrupted response CRC.
    voltage = _normal(contract, "line_a_voltage", 380500, _ts(counter))
    add(_with_response(voltage, corrupt_crc(bytes.fromhex(voltage["response_hex"])).hex()))

    # Corrupted request CRC.
    pressure = _normal(contract, "line_a_pressure", 10130, _ts(counter))
    pressure_record = dict(pressure)
    pressure_record["request_hex"] = corrupt_crc(
        bytes.fromhex(pressure["request_hex"])
    ).hex()
    add(pressure_record)

    # A valid frame pair with no matching point contract.
    no_contract_request = build_request(1, 3, 99, 1).hex()
    no_contract_response = build_response(1, 3, b"\x00\x07").hex()
    add({
        "ts": _ts(counter),
        "request_hex": no_contract_request,
        "response_hex": no_contract_response,
        "source": SOURCE,
    })

    # Truncated response.
    level = _normal(contract, "line_a_level", 225, _ts(counter))
    add(_with_response(level, bytes.fromhex(level["response_hex"])[:-3].hex()))

    # Declared data length does not match the register count in the contract.
    status = _point(contract, "line_a_status")
    add({
        "ts": _ts(counter),
        "request_hex": _request_for(status),
        "response_hex": build_response(
            status.slave_id, status.function, b"\x00\x00\x00\x00"
        ).hex(),
        "source": SOURCE,
    })

    # Response came from a different slave address.
    speed = _point(contract, "line_a_speed")
    add({
        "ts": _ts(counter),
        "request_hex": _request_for(speed),
        "response_hex": build_response(
            9, speed.function, encode_raw(12340, speed.data_type, speed.byte_order)
        ).hex(),
        "source": SOURCE,
    })

    # Exact duplicate of the first reading (same device/point/timestamp).
    duplicate = dict(records[0])
    add(duplicate)

    # line_a_energy is deliberately absent to trigger final missing_response.
    return records


def generate_demo_replay(
    contract_path: str | Path = DEFAULT_CONFIG_PATH,
    output_path: str | Path = DEFAULT_FIXTURE_PATH,
) -> dict[str, Any]:
    contract = load_contract(contract_path)
    records = build_demo_records(contract)
    text = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    target = atomic_write_text(output_path, text)
    return {
        "path": str(target),
        "lines": len(records),
        "sha256": sha256_text(text),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate deterministic pointlog fixtures")
    parser.add_argument("--contract", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--output", default=str(DEFAULT_FIXTURE_PATH))
    args = parser.parse_args(argv)
    result = generate_demo_replay(args.contract, args.output)
    print(f"fixture: {result['path']}")
    print(f"lines:   {result['lines']}")
    print(f"sha256:  {result['sha256']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
