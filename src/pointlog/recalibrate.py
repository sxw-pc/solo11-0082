"""Auditable recalculation of captured readings under late calibration sheets.

Recalculation never updates the original ``readings`` rows. Every conclusion
is appended to ``derived_readings`` with the source batch, the old and new
calibration version ids and a digest of the inputs, so reviewers can replay
why a number changed. Re-running with the same revision is idempotent; a
later revision covering the same interval adds new rows and preserves the
previously derived ones.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import codec
from .contract import ContractSet, PointContract, load_contract, resolve_calibration
from .pipeline import PipelineError
from .quality import (
    CALIBRATION_UNKNOWN,
    DECODE_ERROR,
    QualityFlags,
    determine_quality,
)
from .store import Store
from .util import (
    format_utc,
    normalize_number,
    parse_utc_timestamp,
    sha256_file,
    sha256_text,
    utc_now_iso,
)


class RecalibrationInterrupted(RuntimeError):
    def __init__(self, batch_id: str, considered: int):
        super().__init__(f"recalibration interrupted after {considered} readings")
        self.batch_id = batch_id
        self.considered = considered


@dataclass
class RecalibrationResult:
    batch_id: str
    contract_sha256: str
    considered: int
    unchanged: int
    derived_created: int
    derived_existing: int
    report_paths: dict[str, str] = field(default_factory=dict)


def _input_digest(
    *,
    batch_id: str,
    reading: dict,
    old_version: str,
    new_version: str,
    scale: float | None,
    offset: float | None,
    contract_sha256: str,
) -> str:
    parts = [
        batch_id,
        reading["device_id"],
        reading["point_key"],
        reading["ts"],
        repr(reading["raw_value"]),
        old_version,
        new_version,
        "" if scale is None else repr(scale),
        "" if offset is None else repr(offset),
        contract_sha256,
    ]
    return sha256_text("|".join(parts))


def _derived_conclusion(
    point: PointContract, raw_value: float, engineering: float | None, known: bool
) -> str:
    if not known:
        return CALIBRATION_UNKNOWN
    if engineering is None:
        return DECODE_ERROR
    range_code = codec.classify_range(engineering, point.range_min, point.range_max)
    in_enum, _ = codec.enum_label(point.enum, raw_value)
    return determine_quality(QualityFlags(
        overflow=range_code == "overflow",
        under_range=range_code == "under_range",
        not_in_enum=not in_enum,
    ))


def run_recalibration(
    *,
    db_path: str | Path,
    contract_path: str | Path,
    batch_id: str | None = None,
    reports_dir: str | Path | None = None,
    now_factory: Callable[[], str] = utc_now_iso,
    stop_after: int | None = None,
) -> RecalibrationResult:
    """Recalculate one batch against the contract's latest calibration revision.

    Only points declaring a calibration table are considered. Frame-level
    CRC/protocol failures never produced readings, so they are never
    recalculated; the original frames and raw register values stay as evidence.
    """
    if stop_after is not None and stop_after <= 0:
        raise ValueError("stop_after must be a positive integer")
    db_path = Path(db_path)
    if not db_path.is_file():
        raise PipelineError("db_missing", f"database does not exist: {db_path}")
    contract_path = Path(contract_path)
    contract: ContractSet = load_contract(contract_path)
    contract_sha256 = sha256_file(contract_path)
    store = Store(db_path)
    try:
        if batch_id is None:
            latest = store.find_latest_batch()
            if latest is None:
                raise PipelineError("batch_missing", "no capture batch in this database")
            batch_id = latest["id"]
        if store.get_batch(batch_id) is None:
            raise PipelineError("batch_missing", f"batch {batch_id} does not exist")

        latest_derived = store.latest_derived_map(batch_id)
        considered = 0
        unchanged = 0
        created = 0
        existing = 0
        for reading in store.batch_readings(batch_id):
            point = contract.get(reading["point_key"])
            if point is None or not point.calibrations:
                continue
            considered += 1
            if stop_after is not None and considered > stop_after:
                raise RecalibrationInterrupted(batch_id, considered - 1)

            key = (reading["device_id"], reading["point_key"], reading["ts"])
            current = latest_derived.get(key)
            current_version = (
                current["new_calibration_version_id"]
                if current is not None
                else reading["calibration_version_id"]
            )

            moment = parse_utc_timestamp(reading["ts"])
            resolved = resolve_calibration(point, moment) if moment is not None else None
            new_version_id = ""
            # The consulted revision is always the latest registered one, even
            # when the timestamp falls into a gap or cannot be parsed.
            registered_at = format_utc(point.calibrations[-1].registered_at)
            scale: float | None = None
            offset: float | None = None
            engineering: float | int | None = None
            if resolved is not None:
                _, version = resolved
                new_version_id = version.version_id
                scale, offset = version.scale, version.offset
                try:
                    engineering = float(normalize_number(
                        codec.apply_engineering(reading["raw_value"], scale, offset)
                    ))
                except codec.CodecError:
                    engineering = None

            if new_version_id == current_version:
                unchanged += 1
                continue

            _, enum_label = codec.enum_label(point.enum, reading["raw_value"])
            derived = {
                "device_id": reading["device_id"],
                "point_key": reading["point_key"],
                "ts": reading["ts"],
                "source_reading_id": reading["id"],
                "old_calibration_version_id": current_version,
                "new_calibration_version_id": new_version_id,
                "new_calibration_registered_at": registered_at,
                "scale": scale,
                "offset": offset,
                "raw_value": reading["raw_value"],
                "engineering_value": engineering,
                "quality_code": _derived_conclusion(
                    point, reading["raw_value"], engineering, resolved is not None
                ),
                "enum_label": enum_label,
                "input_sha256": _input_digest(
                    batch_id=batch_id,
                    reading=reading,
                    old_version=current_version,
                    new_version=new_version_id,
                    scale=scale,
                    offset=offset,
                    contract_sha256=contract_sha256,
                ),
                "created_at": now_factory(),
            }
            if store.add_derived_reading(batch_id, derived):
                created += 1
                latest_derived[key] = {"new_calibration_version_id": new_version_id}
            else:
                existing += 1

        report_paths: dict[str, str] = {}
        if reports_dir is not None:
            from .report import write_reports

            report_paths = write_reports(store, batch_id, contract, Path(reports_dir))
        return RecalibrationResult(
            batch_id=batch_id,
            contract_sha256=contract_sha256,
            considered=considered,
            unchanged=unchanged,
            derived_created=created,
            derived_existing=existing,
            report_paths=report_paths,
        )
    finally:
        store.close()
