"""Frozen robust baselines and offline sustained-drift review.

The drift layer is deliberately *separate* from quality classification:
``lineproc`` still records the static quality of every frame, and a value that
stays inside ``contract.range`` remains ``good``.  This module answers a
different question -- "does this point's recent good history differ from a
frozen, trusted reference history?" -- without mutating readings or quality
codes and without asserting that a statistical anomaly is a device fault.

Key properties:

* a baseline is trained from exactly one explicitly named, completed batch;
* only ``quality_code='good'`` rows with legal (timezone-aware) timestamps are
  used, unusable rows are excluded and reported, never coerced to zero;
* the baseline freezes the training replay/contract SHA-256, contract
  calibration signature, algorithm version and the per-point median/MAD;
* reuse re-verifies the training content digest and contract calibration, so a
  changed training input or a changed unit/calibration is rejected;
* sampling is either one global reference per point or one independent
  reference per UTC hour -- an hour with too few samples never falls back to
  the global layer;
* detection uses the frozen robust scale and a configurable ``k * MAD`` band,
  requires consecutive same-direction valid samples, and resets the run on an
  isolated spike, a long gap, an unusable frame or an unavailable hour layer.

All results are derived deterministically from stored rows plus the frozen
baseline, so repeated and resumed reviews produce identical output.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from .contract import ContractSet, PointContract
from .quality import GOOD
from .store import Store

ALGORITHM_VERSION = "robust-mad-v1"
#: MAD -> sigma consistency factor for a normal distribution.
MAD_SCALE = 1.4826
DEFAULT_MIN_SAMPLES = 5
DEFAULT_MAX_GAP_SECONDS = 6 * 60 * 60.0
DEFAULT_THRESHOLD = 3.0
DEFAULT_CONSECUTIVE = 3
SAMPLING_GLOBAL = "global"
SAMPLING_HOURLY = "hourly"
SAMPLINGS = (SAMPLING_GLOBAL, SAMPLING_HOURLY)

BASELINE_SCHEMA_VERSION = 1
DRIFT_REPORT_SCHEMA_VERSION = 1

#: Evidence codes that mean "cannot judge" rather than "judged stable/drifting".
#: They are surfaced next to the verdict so a low-confidence review is never
#: mistaken for a clean one.
INSUFFICIENT_EVIDENCE_CODES = frozenset({
    "no_samples",
    "insufficient_samples",
    "mad_zero_variable",
    "training_gap",
    "no_training_samples",
    "global_layer_insufficient",
    "hour_unavailable",
    "hour_insufficient",
    "hours_insufficient",
    "no_ready_hour",
    "detection_gap",
    "no_detection_samples",
    "no_baseline_layer",
    "invalid_timestamp",
    "invalid_value",
    "unit_mismatch",
    "missing_reading_row",
})


class DriftError(RuntimeError):
    """A drift-review failure with a stable machine-readable code."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class DriftRejected(DriftError):
    """A frozen baseline may not be reused for the requested batch/contract."""


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _num(value: float | int | None) -> float | None:
    if value is None:
        return None
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0 else rounded


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a legal, timezone-aware timestamp into UTC.

    Naive or malformed timestamps are *invalid* rather than assumed to be UTC:
    an unusable sample must never be silently treated as a normal sample.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Contract calibration fingerprint
# ---------------------------------------------------------------------------


def calibration_signature(point: PointContract) -> str:
    """Fingerprint the semantic calibration of one point.

    Unit, scale/offset, data type and byte order decide what an engineering
    value *means*; range/enum/labels are deliberately excluded so that purely
    presentation-level contract edits do not invalidate a frozen baseline.
    """
    return _digest({
        "device_id": point.device_id,
        "point_key": point.point_key,
        "data_type": point.data_type,
        "byte_order": point.byte_order,
        "scale": float(point.scale),
        "offset": float(point.offset),
        "unit": point.unit,
    })


def contract_calibration(contract: ContractSet) -> dict[str, str]:
    return {
        point.point_key: calibration_signature(point)
        for point in sorted(contract.points, key=lambda item: item.point_key)
    }


# ---------------------------------------------------------------------------
# Row loading
# ---------------------------------------------------------------------------


def _training_rows(store: Store, batch_id: str) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT device_id, point_key, ts, line_no, engineering_value, "
        "quality_code, unit FROM readings WHERE batch_id = ? "
        "ORDER BY device_id, point_key, ts, line_no",
        (batch_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _detection_observations(
    store: Store, batch_id: str, device_id: str, point_key: str
) -> list[dict[str, Any]]:
    """Frames for one point joined with their reading, as a timeline.

    Frames are the authoritative per-line record: bad CRC, missing response and
    other unusable frames produce no reading row, so reading rows alone could
    not reveal where a contiguous run must be broken.
    """
    rows = store.conn.execute(
        "SELECT f.line_no, f.ts, f.quality_code AS frame_quality, "
        "f.device_id, f.point_key, r.engineering_value, r.unit, "
        "r.quality_code AS reading_quality "
        "FROM frames f LEFT JOIN readings r "
        "  ON r.batch_id = f.batch_id AND r.device_id = f.device_id "
        " AND r.point_key = f.point_key AND r.ts = f.ts "
        "WHERE f.batch_id = ? AND f.device_id = ? AND f.point_key = ?",
        (batch_id, device_id, point_key),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Layer statistics
# ---------------------------------------------------------------------------


def _compute_layer(
    samples: list[dict[str, Any]],
    *,
    min_samples: int,
    max_gap_seconds: float,
    hour: int | None = None,
) -> dict[str, Any]:
    count = len(samples)
    layer: dict[str, Any] = {
        "hour": hour,
        "sample_count": count,
        "status": "insufficient",
        "reason": None,
        "median": None,
        "mad": None,
        "robust_scale": None,
        "constant": False,
        "reference_start": None,
        "reference_end": None,
        "max_gap_seconds": None,
    }
    if count == 0:
        layer["reason"] = "no_samples"
        return layer

    values = [float(sample["value"]) for sample in samples]
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    layer.update({
        "median": _num(median),
        "mad": _num(mad),
        "robust_scale": _num(MAD_SCALE * mad),
        "constant": all(value == values[0] for value in values),
        "reference_start": samples[0]["ts"],
        "reference_end": samples[-1]["ts"],
    })
    if count >= 2:
        gaps = [
            (samples[index]["dt"] - samples[index - 1]["dt"]).total_seconds()
            for index in range(1, count)
        ]
        layer["max_gap_seconds"] = _num(max(gaps))
    else:
        layer["max_gap_seconds"] = 0.0

    if count < min_samples:
        layer["reason"] = "insufficient_samples"
        return layer
    if mad == 0 and not layer["constant"]:
        # Zero robust dispersion over non-constant data makes the band
        # degenerate and the "3 x MAD" threshold meaningless.
        layer["reason"] = "mad_zero_variable"
        return layer
    if (layer["max_gap_seconds"] or 0.0) > max_gap_seconds:
        layer["reason"] = "training_gap"
        return layer
    layer["status"] = "ready"
    return layer


def _excluded_summary(excluded: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in excluded:
        entry = grouped.setdefault(item["reason"], {
            "reason": item["reason"],
            "count": 0,
            "line_numbers": [],
        })
        entry["count"] += 1
        if len(entry["line_numbers"]) < 10:
            entry["line_numbers"].append(item["line_no"])
    result = []
    for reason in sorted(grouped):
        entry = grouped[reason]
        entry["line_numbers"].sort()
        result.append(entry)
    return result


def _sample_reason(row: dict[str, Any], point: PointContract) -> str | None:
    if row["quality_code"] != GOOD:
        return "quality_not_good"
    if parse_timestamp(row["ts"]) is None:
        return "invalid_timestamp"
    try:
        value = float(row["engineering_value"])
    except (TypeError, ValueError):
        return "invalid_value"
    if not math.isfinite(value):
        return "invalid_value"
    if row["unit"] != point.unit:
        return "unit_mismatch"
    return None


# ---------------------------------------------------------------------------
# Baseline construction and persistence
# ---------------------------------------------------------------------------


def _training_digest(
    *,
    batch: dict[str, Any],
    rows: list[dict[str, Any]],
    calibration: dict[str, str],
    algorithm_version: str,
) -> str:
    """Fingerprint exactly *what was trained on*, not the derived parameters.

    Sampling semantics and thresholds are captured by the baseline id instead,
    so two baselines built from the same frozen training content share this
    digest and can be compared directly.
    """
    material = {
        "algorithm_version": algorithm_version,
        "training_batch": {
            "id": batch["id"],
            "replay_sha256": batch["replay_sha256"],
            "contract_sha256": batch["contract_sha256"],
            "started_at": batch["started_at"],
            "finished_at": batch["finished_at"],
            "status": batch["status"],
            "total_lines": int(batch["total_lines"]),
        },
        "contract_calibration": calibration,
        "readings": [
            [
                row["device_id"],
                row["point_key"],
                row["ts"],
                int(row["line_no"]),
                float(row["engineering_value"]),
                row["quality_code"],
                row["unit"],
            ]
            for row in rows
        ],
    }
    return _digest(material)


def _baseline_id(
    training_digest: str,
    *,
    sampling: str,
    algorithm_version: str,
    min_samples: int,
    max_gap_seconds: float,
) -> str:
    return "bl_" + _digest({
        "training_digest": training_digest,
        "sampling": sampling,
        "algorithm_version": algorithm_version,
        "min_samples": min_samples,
        "max_gap_seconds": max_gap_seconds,
    })[:16]


def build_baseline(
    store: Store,
    *,
    training_batch_id: str,
    contract: ContractSet,
    sampling: str = SAMPLING_GLOBAL,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    algorithm_version: str = ALGORITHM_VERSION,
) -> dict[str, Any]:
    """Freeze a baseline from one explicitly named, completed batch."""
    if sampling not in SAMPLINGS:
        raise DriftError("invalid_sampling", f"sampling must be one of {SAMPLINGS}")
    if min_samples < 1:
        raise DriftError("invalid_min_samples", "min_samples must be >= 1")
    if not math.isfinite(max_gap_seconds) or max_gap_seconds <= 0:
        raise DriftError("invalid_max_gap", "max_gap_seconds must be positive")

    batch = store.get_batch(training_batch_id)
    if batch is None:
        raise DriftError(
            "training_batch_missing", f"training batch {training_batch_id} does not exist"
        )
    if batch["status"] != "completed":
        raise DriftError(
            "training_batch_incomplete",
            f"training batch {training_batch_id} is {batch['status']}; "
            "a frozen baseline requires a completed batch",
        )

    rows = _training_rows(store, training_batch_id)
    calibration = contract_calibration(contract)
    training_digest = _training_digest(
        batch=batch,
        rows=rows,
        calibration=calibration,
        algorithm_version=algorithm_version,
    )
    rows_by_point: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_point[(row["device_id"], row["point_key"])].append(row)

    points: list[dict[str, Any]] = []
    for point in sorted(contract.points, key=lambda item: (item.device_id, item.point_key)):
        point_rows = rows_by_point.get((point.device_id, point.point_key), [])
        valid: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for row in point_rows:
            reason = _sample_reason(row, point)
            if reason is not None:
                excluded.append({"reason": reason, "line_no": int(row["line_no"])})
                continue
            parsed = parse_timestamp(row["ts"])
            assert parsed is not None  # _sample_reason verified it
            valid.append({
                "dt": parsed,
                "ts": row["ts"],
                "line_no": int(row["line_no"]),
                "value": float(row["engineering_value"]),
            })
        valid.sort(key=lambda item: (item["dt"], item["line_no"]))

        entry: dict[str, Any] = {
            "device_id": point.device_id,
            "point_key": point.point_key,
            "unit": point.unit,
            "calibration": calibration.get(point.point_key),
            "status": "insufficient",
            "sample_count": len(valid),
            "excluded": _excluded_summary(excluded),
            "evidence": [],
            "median": None,
            "mad": None,
            "robust_scale": None,
            "reference_start": None,
            "reference_end": None,
            "max_gap_seconds": None,
            "hours": {},
        }

        if sampling == SAMPLING_GLOBAL:
            layer = _compute_layer(
                valid, min_samples=min_samples, max_gap_seconds=max_gap_seconds
            )
            entry.update({
                "status": layer["status"],
                "median": layer["median"],
                "mad": layer["mad"],
                "robust_scale": layer["robust_scale"],
                "constant": layer["constant"],
                "reference_start": layer["reference_start"],
                "reference_end": layer["reference_end"],
                "max_gap_seconds": layer["max_gap_seconds"],
            })
            if layer["status"] != "ready":
                entry["evidence"].append({
                    "code": layer["reason"],
                    "detail": f"global layer has {layer['sample_count']} usable sample(s)",
                })
        else:
            by_hour: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for sample in valid:
                by_hour[sample["dt"].hour].append(sample)
            ready_hours: list[str] = []
            for hour in sorted(by_hour):
                hour_layer = _compute_layer(
                    by_hour[hour],
                    min_samples=min_samples,
                    max_gap_seconds=max_gap_seconds,
                    hour=hour,
                )
                entry["hours"][f"{hour:02d}"] = hour_layer
                if hour_layer["status"] == "ready":
                    ready_hours.append(f"{hour:02d}")
            # A reference end/start is still useful provenance even when the
            # point is only partially covered.
            if valid:
                entry["reference_start"] = valid[0]["ts"]
                entry["reference_end"] = valid[-1]["ts"]
            if ready_hours:
                entry["status"] = "ready"
            else:
                entry["evidence"].append({
                    "code": "no_ready_hour",
                    "detail": "no UTC hour has enough usable training samples",
                })
            insufficient_hours = [
                hour for hour, layer in entry["hours"].items()
                if layer["status"] != "ready"
            ]
            if insufficient_hours:
                entry["evidence"].append({
                    "code": "hours_insufficient",
                    "detail": "hours without a ready reference: "
                              + ", ".join(insufficient_hours),
                })

        if not valid:
            entry["evidence"].append({
                "code": "no_training_samples",
                "detail": "no good, timestamp-valid training sample for this point",
            })
        if excluded:
            entry["evidence"].append({
                "code": "excluded_samples",
                "detail": "unusable training rows were excluded, not treated as normal",
            })
        points.append(entry)

    payload: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "algorithm_version": algorithm_version,
        "sampling": sampling,
        "params": {
            "min_samples": min_samples,
            "max_gap_seconds": _num(max_gap_seconds),
            "mad_scale": MAD_SCALE,
            "threshold": DEFAULT_THRESHOLD,
            "consecutive": DEFAULT_CONSECUTIVE,
        },
        "training_batch": {
            "id": batch["id"],
            "replay_sha256": batch["replay_sha256"],
            "contract_sha256": batch["contract_sha256"],
            "contract_file": str(batch["contract_path"]),
            "started_at": batch["started_at"],
            "finished_at": batch["finished_at"],
            "status": batch["status"],
            "total_lines": int(batch["total_lines"]),
        },
        "contract_calibration": calibration,
        "training_digest": training_digest,
        "points": points,
    }
    payload["id"] = _baseline_id(
        training_digest,
        sampling=sampling,
        algorithm_version=algorithm_version,
        min_samples=min_samples,
        max_gap_seconds=float(max_gap_seconds),
    )
    return payload


def save_baseline(store: Store, baseline: dict[str, Any]) -> str:
    payload = _canonical(baseline)
    with store.conn:
        store.conn.execute(
            "INSERT OR IGNORE INTO drift_baselines("
            "id, algorithm_version, sampling, training_batch_id, "
            "training_replay_sha256, training_contract_sha256, contract_signature, "
            "training_digest, min_samples, max_gap_seconds, created_at, payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                baseline["id"],
                baseline["algorithm_version"],
                baseline["sampling"],
                baseline["training_batch"]["id"],
                baseline["training_batch"]["replay_sha256"],
                baseline["training_batch"]["contract_sha256"],
                _digest(baseline["contract_calibration"]),
                baseline["training_digest"],
                int(baseline["params"]["min_samples"]),
                float(baseline["params"]["max_gap_seconds"]),
                _iso_utc(datetime.now(timezone.utc)),
                payload,
            ),
        )
    return str(baseline["id"])


def get_baseline(store: Store, baseline_id: str) -> dict[str, Any]:
    row = store.conn.execute(
        "SELECT payload FROM drift_baselines WHERE id = ?", (baseline_id,)
    ).fetchone()
    if row is None:
        raise DriftError("baseline_missing", f"baseline {baseline_id} does not exist")
    return json.loads(row["payload"])


def list_baselines(store: Store) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT id, algorithm_version, sampling, training_batch_id, "
        "training_digest, min_samples, max_gap_seconds, created_at "
        "FROM drift_baselines ORDER BY created_at, id"
    ).fetchall()
    return [dict(row) for row in rows]


def _baseline_points(baseline: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (point["device_id"], point["point_key"]): point
        for point in baseline["points"]
    }


def verify_baseline(
    store: Store,
    baseline: dict[str, Any],
    contract: ContractSet,
) -> None:
    """Reject reuse when training input or contract calibration changed."""
    if baseline.get("algorithm_version") != ALGORITHM_VERSION:
        raise DriftRejected(
            "algorithm_changed",
            f"baseline algorithm {baseline.get('algorithm_version')!r} cannot be "
            f"reused by {ALGORITHM_VERSION!r}",
        )
    training = baseline["training_batch"]
    batch = store.get_batch(training["id"])
    if batch is None:
        raise DriftRejected(
            "training_input_changed",
            f"training batch {training['id']} no longer exists",
        )
    if (
        batch["status"] != "completed"
        or batch["replay_sha256"] != training["replay_sha256"]
        or batch["contract_sha256"] != training["contract_sha256"]
    ):
        raise DriftRejected(
            "training_input_changed",
            "training batch fingerprint or completion status changed",
        )

    stored_calibration = baseline["contract_calibration"]
    current = contract_calibration(contract)
    for point_key, signature in stored_calibration.items():
        if current.get(point_key) != signature:
            raise DriftRejected(
                "contract_changed",
                f"contract unit/calibration for point {point_key} changed; "
                "refusing to reuse the frozen baseline",
            )

    rows = _training_rows(store, training["id"])
    recomputed = _training_digest(
        batch=batch,
        rows=rows,
        calibration=stored_calibration,
        algorithm_version=baseline["algorithm_version"],
    )
    if recomputed != baseline["training_digest"]:
        raise DriftRejected(
            "training_input_changed",
            "training readings changed since the baseline was frozen",
        )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class _Evidence:
    """Aggregate repeated evidence codes deterministically."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    def add(
        self,
        code: str,
        detail: str,
        *,
        line_no: int | None = None,
        ts: str | None = None,
    ) -> None:
        item = self._items.setdefault(code, {
            "code": code,
            "count": 0,
            "line_numbers": [],
            "timestamps": [],
            "detail": detail,
        })
        item["count"] += 1
        if line_no is not None and len(item["line_numbers"]) < 10:
            item["line_numbers"].append(int(line_no))
        if ts is not None and len(item["timestamps"]) < 5:
            item["timestamps"].append(ts)

    def items(self) -> list[dict[str, Any]]:
        result = []
        for code in sorted(self._items):
            item = self._items[code]
            item["line_numbers"].sort()
            result.append(item)
        return result


def _resolve_layer(
    baseline_point: dict[str, Any],
    sampling: str,
    moment: datetime,
) -> tuple[dict[str, Any] | None, str, str | None]:
    """Return ``(layer, label, insufficient_reason)`` for one sample."""
    if sampling == SAMPLING_GLOBAL:
        if baseline_point["status"] != "ready":
            reason = "global_layer_insufficient"
            for item in baseline_point.get("evidence", []):
                if item["code"] in {
                    "no_samples", "insufficient_samples", "mad_zero_variable",
                    "training_gap", "no_training_samples",
                }:
                    reason = item["code"]
                    break
            return None, "global", reason
        return baseline_point, "global", None

    hour_label = f"{moment.hour:02d}"
    layer = baseline_point.get("hours", {}).get(hour_label)
    if layer is None:
        return None, f"hour:{hour_label}", "hour_unavailable"
    if layer["status"] != "ready":
        return None, f"hour:{hour_label}", layer["reason"] or "hour_insufficient"
    return layer, f"hour:{hour_label}", None


def _layer_summary(layer: dict[str, Any], label: str) -> dict[str, Any]:
    return {
        "reference": label,
        "sample_count": int(layer["sample_count"]),
        "median": layer["median"],
        "mad": layer["mad"],
        "robust_scale": layer["robust_scale"],
        "reference_start": layer["reference_start"],
        "reference_end": layer["reference_end"],
    }


def _direction(
    value: float, layer: dict[str, Any], threshold: float
) -> tuple[int, float]:
    """Classify one sample against a frozen layer using ``threshold x MAD``.

    ``MAD`` is the robust dispersion statistic frozen in the baseline;
    ``robust_scale`` (``1.4826 x MAD``) is reported alongside it as the
    sigma-equivalent but the configured multiplier applies to MAD directly.
    """
    median = float(layer["median"])
    deviation = value - median
    mad = float(layer["mad"] or 0.0)
    if mad > 0:
        band = threshold * mad
        if deviation > band:
            return 1, deviation
        if deviation < -band:
            return -1, deviation
        return 0, deviation
    # Constant reference: MAD is 0 by definition; only exact equality is normal.
    if deviation > 0:
        return 1, deviation
    if deviation < 0:
        return -1, deviation
    return 0, deviation


def _analyze_point(
    baseline_point: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    sampling: str,
    threshold: float,
    consecutive: int,
    max_gap_seconds: float,
) -> dict[str, Any]:
    evidence = _Evidence()
    events: list[dict[str, Any]] = []
    layers_used: set[str] = set()
    ordered = sorted(
        observations,
        key=lambda item: (
            parse_timestamp(item["ts"]) or datetime.min.replace(tzinfo=timezone.utc),
            int(item["line_no"]),
        ),
    )

    run: list[dict[str, Any]] = []
    run_direction = 0
    run_layer: str | None = None
    in_episode = False
    previous_dt: datetime | None = None
    valid_count = 0
    invalid_count = 0
    insufficient = False

    for observation in ordered:
        moment = parse_timestamp(observation["ts"])
        reason: str | None = None
        value: float | None = None
        if observation["frame_quality"] != GOOD:
            reason = f"frame_{observation['frame_quality']}"
        elif observation["reading_quality"] is None:
            reason = "missing_reading_row"
        elif observation["reading_quality"] != GOOD:
            reason = f"reading_{observation['reading_quality']}"
        elif moment is None:
            reason = "invalid_timestamp"
        else:
            try:
                value = float(observation["engineering_value"])
            except (TypeError, ValueError):
                value = None
            if value is None or not math.isfinite(value):
                reason = "invalid_value"
            elif observation["unit"] != baseline_point["unit"]:
                reason = "unit_mismatch"

        if reason is not None:
            invalid_count += 1
            evidence.add(
                reason,
                "unusable sample breaks the contiguous valid run",
                line_no=observation["line_no"],
                ts=observation["ts"] or None,
            )
            run = []
            run_direction = 0
            run_layer = None
            in_episode = False
            previous_dt = None
            continue

        assert moment is not None and value is not None
        valid_count += 1
        if previous_dt is not None:
            gap = (moment - previous_dt).total_seconds()
            if gap > max_gap_seconds:
                insufficient = True
                evidence.add(
                    "detection_gap",
                    f"{_num(gap)}s gap exceeds max_gap_seconds={_num(max_gap_seconds)}",
                    line_no=observation["line_no"],
                    ts=observation["ts"],
                )
                run = []
                run_direction = 0
                run_layer = None
                in_episode = False
        previous_dt = moment

        layer, label, layer_reason = _resolve_layer(baseline_point, sampling, moment)
        if layer is None:
            # An unavailable hour layer is *not* silently replaced by the
            # global reference; the sample simply cannot be judged.
            insufficient = True
            evidence.add(
                layer_reason or "layer_unavailable",
                f"no usable reference layer ({label}) for this sample",
                line_no=observation["line_no"],
                ts=observation["ts"],
            )
            run = []
            run_direction = 0
            run_layer = None
            in_episode = False
            continue
        layers_used.add(label)

        direction, deviation = _direction(value, layer, threshold)
        if direction == 0:
            run = []
            run_direction = 0
            run_layer = None
            in_episode = False
            continue
        if direction != run_direction or label != run_layer:
            # A direction flip, a layer switch (e.g. crossing a UTC hour in
            # hourly mode) or a same-direction run after a normal sample all
            # start a fresh episode; a run must stay on one reference layer.
            run = []
            run_direction = direction
            run_layer = label
            in_episode = False
        if not in_episode:
            # Once an episode is reported the remaining same-direction samples
            # add no new information, so the buffer stays bounded.
            run.append({
                "ts": _iso_utc(moment),
                "line_no": int(observation["line_no"]),
                "value": _num(value),
                "deviation": _num(deviation),
                "direction": "up" if direction > 0 else "down",
                "reference": label,
            })

        # One event per contiguous episode: the run is reported when it first
        # reaches ``consecutive`` and is not re-fired for every later sample.
        if not in_episode and len(run) >= consecutive:
            window = run[:consecutive]
            detected = window[-1]
            events.append({
                "device_id": baseline_point["device_id"],
                "point_key": baseline_point["point_key"],
                "unit": baseline_point["unit"],
                "direction": detected["direction"],
                "start": window[0],
                "detected_at": detected,
                "line_numbers": [sample["line_no"] for sample in window],
                "magnitude": _num(abs(detected["deviation"])),
                "deviation": detected["deviation"],
                "reference": label,
                "baseline": _layer_summary(layer, label),
            })
            in_episode = True

    if invalid_count and valid_count == 0:
        status = "insufficient"
        insufficient = True
    elif not ordered:
        status = "insufficient"
        insufficient = True
        evidence.add(
            "no_detection_samples",
            "the detection batch has no frame for this point",
        )
    elif events:
        status = "drift"
    elif insufficient:
        status = "insufficient"
    else:
        status = "stable"

    reference = SAMPLING_GLOBAL if sampling == SAMPLING_GLOBAL else SAMPLING_HOURLY
    return {
        "device_id": baseline_point["device_id"],
        "point_key": baseline_point["point_key"],
        "unit": baseline_point["unit"],
        "reference": reference,
        "layers_used": sorted(layers_used),
        "status": status,
        "insufficient": insufficient,
        "sample_count": valid_count,
        "invalid_sample_count": invalid_count,
        "evidence": evidence.items(),
        "drifts": events,
    }


def _detection_batch_meta(batch: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": batch["id"],
        "status": batch["status"],
        "replay_sha256": batch["replay_sha256"],
        "contract_sha256": batch["contract_sha256"],
        "started_at": batch["started_at"],
        "finished_at": batch["finished_at"],
        "total_lines": int(batch["total_lines"]),
        "processed_lines": int(batch["processed_lines"]),
    }


def detect_drift(
    store: Store,
    *,
    baseline: dict[str, Any],
    detection_batch_id: str,
    contract: ContractSet,
    threshold: float = DEFAULT_THRESHOLD,
    consecutive: int = DEFAULT_CONSECUTIVE,
) -> dict[str, Any]:
    """Compare one detection batch against a frozen baseline.

    The detection batch is read only; it never updates the reference.  A batch
    that was used for training may not also be used as the detection batch.
    """
    if not math.isfinite(threshold) or threshold <= 0:
        raise DriftError("invalid_threshold", "threshold must be positive and finite")
    if consecutive < 1:
        raise DriftError("invalid_consecutive", "consecutive must be >= 1")
    verify_baseline(store, baseline, contract)

    if detection_batch_id == baseline["training_batch"]["id"]:
        raise DriftRejected(
            "detection_is_training",
            "the detection batch is the frozen training batch; "
            "it cannot also be used as evidence",
        )
    detection_batch = store.get_batch(detection_batch_id)
    if detection_batch is None:
        raise DriftError(
            "detection_batch_missing",
            f"detection batch {detection_batch_id} does not exist",
        )
    if detection_batch["status"] != "completed":
        raise DriftError(
            "detection_batch_incomplete",
            f"detection batch {detection_batch_id} is {detection_batch['status']}; "
            "drift review requires a completed batch",
        )

    baseline_map = _baseline_points(baseline)
    contract_units = {
        (point.device_id, point.point_key): point.unit for point in contract.points
    }
    point_keys = set(baseline_map) | set(contract_units)
    max_gap_seconds = float(baseline["params"]["max_gap_seconds"])

    results: list[dict[str, Any]] = []
    for device_id, point_key in sorted(point_keys):
        baseline_point = baseline_map.get((device_id, point_key))
        if baseline_point is None:
            results.append({
                "device_id": device_id,
                "point_key": point_key,
                "unit": contract_units.get((device_id, point_key), ""),
                "reference": baseline["sampling"],
                "layers_used": [],
                "status": "insufficient",
                "insufficient": True,
                "sample_count": 0,
                "invalid_sample_count": 0,
                "evidence": [{
                    "code": "no_baseline_layer",
                    "count": 1,
                    "line_numbers": [],
                    "timestamps": [],
                    "detail": "point is absent from the frozen baseline",
                }],
                "drifts": [],
            })
            continue
        observations = _detection_observations(
            store, detection_batch_id, device_id, point_key
        )
        results.append(_analyze_point(
            baseline_point,
            observations,
            sampling=baseline["sampling"],
            threshold=threshold,
            consecutive=consecutive,
            max_gap_seconds=max_gap_seconds,
        ))

    drift_points = [item for item in results if item["drifts"]]
    insufficient_points = [item for item in results if item["status"] == "insufficient"]
    stable_points = [item for item in results if item["status"] == "stable"]
    return {
        "schema_version": DRIFT_REPORT_SCHEMA_VERSION,
        "algorithm_version": baseline["algorithm_version"],
        "sampling": baseline["sampling"],
        "params": {
            "threshold": _num(threshold),
            "consecutive": consecutive,
            "mad_scale": baseline["params"]["mad_scale"],
            "min_samples": baseline["params"]["min_samples"],
            "max_gap_seconds": baseline["params"]["max_gap_seconds"],
        },
        "baseline": {
            "id": baseline["id"],
            "training_batch": baseline["training_batch"],
            "training_digest": baseline["training_digest"],
            "contract_calibration_digest": _digest(baseline["contract_calibration"]),
            "reference_start": min(
                (point["reference_start"] for point in baseline["points"]
                 if point["reference_start"]),
                default=None,
            ),
            "reference_end": max(
                (point["reference_end"] for point in baseline["points"]
                 if point["reference_end"]),
                default=None,
            ),
        },
        "detection_batch": _detection_batch_meta(detection_batch),
        "points": results,
        "summary": {
            "points_total": len(results),
            "drift_points": len(drift_points),
            "stable_points": len(stable_points),
            "insufficient_points": len(insufficient_points),
            "drift_events": sum(len(item["drifts"]) for item in results),
            "insufficient_evidence": sum(
                1 for item in results if item["insufficient"]
            ),
        },
    }
