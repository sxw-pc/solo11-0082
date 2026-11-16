"""Offline two-point sustained-interlock evaluation.

This module answers a question the single-point quality ledger cannot answer:
"were two *trusted* readings simultaneously outside their limits for long
enough, and when is that simply unknowable?"  It never sends commands to a
device and never treats missing or bad evidence as a normal value.

Evidence rules
--------------
Only ``readings.quality_code == 'good'`` rows take part.  That quality already
means the request/response CRC, the frame structure, decoding, the configured
range and any enum mapping all passed.  A reading whose timestamp cannot be
parsed is ignored because it cannot be placed on the timeline.

Every other frame that is attributable to one of the rule's points and
carries an untrustworthy value (bad CRC, bad frame, exception response,
decode failure, overflow/under-range, out-of-enum, contradictory duplicate) is
*blocking evidence*: from its timestamp the point's state is unknown until the
next trusted sample.  Two trusted values for the same point at the same
timestamp that disagree, or a trusted sample and a blocking frame at the same
timestamp, are also conflicting evidence.  An empty response is different: it
is the *absence* of a new sample, so a still-valid bounded hold continues past
it to the next sample or the stale limit rather than being cut.

Two sampling semantics are supported and authorised by the contract:

``bounded_hold``
    Each point's most recent trusted sample holds until the next trusted
    sample, the configured stale limit, or a bad-evidence timestamp, whichever
    comes first.  A hold never crosses a bad frame or an explicit normal
    sample.

``strict_simultaneous``
    A state is confirmed only at an instant where both points have a trusted
    sample at the *same* timestamp.  No duration is inferred between discrete
    observations, so a positive ``min_duration_minutes`` can never be met:
    the evaluator reports the confirmed instants plus the unknown gaps
    instead of claiming an event.

A sustained event is only produced when the three-valued timeline is
continuously ``true`` for at least ``min_duration_minutes``.  A short normal
value, a gap, a stale hold or any bad evidence interrupts the clock, so two
separate excursions are never added together.

The evaluator is a pure function of the batch rows.  It sorts by timestamp and
line number, computes deterministic rows, and the caller replaces the batch's
derived rows in one transaction.  The same replay/contract fingerprints
therefore always produce the same intervals and event keys, no matter in which
line order the replay was ingested or how often a resume re-ran the tail.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .contract import (
    DIRECTION_ABOVE,
    ContractSet,
    SAMPLING_STRICT_SIMULTANEOUS,
    InterlockPoint,
    InterlockRule,
)
from .store import Store
from .util import sha256_text

STATE_TRUE = "true"
STATE_FALSE = "false"
STATE_UNKNOWN = "unknown"

REASON_BOTH_TRUE = "both_conditions_true"
REASON_EXPLICIT_NORMAL = "explicit_normal"
REASON_CONFLICT = "conflicting_values"
REASON_BAD_EVIDENCE = "bad_evidence"
REASON_STALE = "stale_evidence"
REASON_GAP = "insufficient_evidence"
REASON_SIMULTANEOUS = "simultaneous_sample"
REASON_NO_SIMULTANEOUS = "no_simultaneous_sample"
REASON_END = "end_of_evidence"

EPS = 1e-9


def parse_timestamp(value: str) -> float | None:
    """Return a UTC epoch for an ISO-8601 timestamp, or ``None``.

    Naive timestamps are interpreted as UTC so that the result does not depend
    on the host timezone.  Anything unparseable is excluded from the timeline.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def format_epoch(epoch: float) -> str:
    """Format an epoch as a stable UTC ISO-8601 string with a trailing Z."""
    moment = datetime.fromtimestamp(epoch, timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Sample:
    """One trusted reading on the timeline."""

    epoch: float
    ts: str
    line_no: int
    value: float


@dataclass(frozen=True)
class BadEvidence:
    """A frame that constrains what can be judged around its timestamp.

    ``blocks_hold`` is true for evidence that makes the value untrustworthy
    (bad CRC/frame, decode failure, exception, conflicting values, an
    out-of-range or out-of-enum value).  Those interrupt a bounded hold
    immediately.  A missing response is merely the absence of a new sample;
    it does not stop an otherwise valid hold from continuing to the next
    sample or the stale limit.
    """

    epoch: float
    ts: str
    line_no: int
    reason: str
    blocks_hold: bool = True

    @property
    def blocking(self) -> bool:
        return self.blocks_hold


@dataclass
class RuleEvaluation:
    rule: InterlockRule
    intervals: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    window_start_ts: str | None = None
    window_end_ts: str | None = None
    window_seconds: float = 0.0

    @property
    def totals(self) -> dict[str, Any]:
        seconds = {STATE_TRUE: 0.0, STATE_FALSE: 0.0, STATE_UNKNOWN: 0.0}
        counts = {STATE_TRUE: 0, STATE_FALSE: 0, STATE_UNKNOWN: 0}
        proven_instants = 0
        for interval in self.intervals:
            seconds[interval["state"]] += interval["duration_seconds"]
            counts[interval["state"]] += 1
            if (interval["state"] == STATE_TRUE
                    and interval["reason"] == REASON_SIMULTANEOUS):
                proven_instants += 1
        return {
            "proven_interlock_seconds": round(seconds[STATE_TRUE], 6),
            "proven_normal_seconds": round(seconds[STATE_FALSE], 6),
            "unknown_seconds": round(seconds[STATE_UNKNOWN], 6),
            "true_interval_count": counts[STATE_TRUE],
            "false_interval_count": counts[STATE_FALSE],
            "unknown_interval_count": counts[STATE_UNKNOWN],
            "proven_simultaneous_instants": proven_instants,
            "event_count": len(self.events),
            "window_seconds": round(self.window_seconds, 6),
        }


def _merge_evidence(
    samples: Iterable[Sample], bad: Iterable[BadEvidence]
) -> tuple[list[Sample], list[BadEvidence]]:
    """Resolve conflicting evidence at the same instant deterministically."""
    samples_by_epoch: dict[float, list[Sample]] = {}
    bad_by_epoch: dict[float, list[BadEvidence]] = {}
    for sample in samples:
        samples_by_epoch.setdefault(sample.epoch, []).append(sample)
    for evidence in bad:
        bad_by_epoch.setdefault(evidence.epoch, []).append(evidence)

    kept: list[Sample] = []
    dropped: list[BadEvidence] = []
    for epoch, group in samples_by_epoch.items():
        # Multiple trusted samples for the same instant are only acceptable if
        # they agree; the ledger normally prevents this with its unique key,
        # but the evaluator must stay correct for hand-built inputs too.
        values = {round(sample.value, 9) for sample in group}
        conflict = (
            any(item.blocking for item in bad_by_epoch.get(epoch, ()))
            or len(values) > 1
        )
        if conflict:
            for sample in group:
                dropped.append(BadEvidence(
                    epoch=epoch, ts=sample.ts, line_no=sample.line_no,
                    reason=REASON_CONFLICT,
                ))
            continue
        group.sort(key=lambda sample: (sample.line_no, sample.ts))
        kept.append(group[0])
    kept.sort(key=lambda sample: (sample.epoch, sample.line_no))
    merged_bad = list(bad)
    merged_bad.extend(dropped)
    merged_bad.sort(key=lambda item: (item.epoch, item.line_no, item.reason))
    return kept, merged_bad


def _point_state(
    samples: list[Sample],
    blocking_epochs: list[float],
    epoch: float,
    stale_seconds: float,
) -> Sample | None:
    """Return the sample that is valid at ``epoch``, or ``None``.

    ``blocking_epochs`` must contain only bad values/frames, never a mere
    missing response.
    """
    times = [sample.epoch for sample in samples]
    index = bisect.bisect_right(times, epoch + EPS) - 1
    if index < 0:
        return None
    sample = samples[index]
    age = epoch - sample.epoch
    if age >= stale_seconds - EPS:
        return None
    # A bad frame after the sample (but not after ``epoch``) invalidates it.
    bad_index = bisect.bisect_right(blocking_epochs, sample.epoch + EPS)
    if bad_index < len(blocking_epochs) and blocking_epochs[bad_index] <= epoch + EPS:
        return None
    return sample


def _combined_state(
    points: tuple[InterlockPoint, InterlockPoint],
    states: tuple[Sample | None, Sample | None],
) -> str:
    known = []
    for point, sample in zip(points, states):
        if sample is None:
            known.append(None)
        else:
            known.append(point.is_triggered(sample.value))
    # Three-valued AND: an explicit false on either side is a proven normal
    # value even if the other side is unknown.
    if any(flag is False for flag in known):
        return STATE_FALSE
    if all(flag is True for flag in known):
        return STATE_TRUE
    return STATE_UNKNOWN


def _boundaries(
    samples: list[Sample], bad: list[BadEvidence],
    stale_seconds: float, start: float, end: float,
) -> list[float]:
    values = {start, end}
    for sample in samples:
        values.add(min(max(sample.epoch, start), end))
        values.add(min(sample.epoch + stale_seconds, end))
    for evidence in bad:
        if evidence.blocking:
            values.add(min(max(evidence.epoch, start), end))
    return sorted(values)


def _interval_evidence_lines(
    start: float,
    end: float,
    samples: list[Sample],
    bad: list[BadEvidence],
    extra_lines: Iterable[int] = (),
) -> list[int]:
    """Line numbers that actually support or contradict ``[start, end)``.

    Samples inside the half-open interval are direct evidence.  A sample that
    merely *bounded* an interval (the last value before it, whose hold was
    ended by the interval's first sample) is not attributed to it.
    """
    lines: set[int] = set(extra_lines)
    for sample in samples:
        if start - EPS <= sample.epoch < end - EPS:
            lines.add(sample.line_no)
    for evidence in bad:
        if start - EPS <= evidence.epoch < end - EPS:
            lines.add(evidence.line_no)
    return sorted(lines)


def _unknown_reason(
    start: float,
    end: float,
    bad: list[BadEvidence],
    per_point_samples: dict[str, list[Sample]] | None = None,
    stale_seconds: float | None = None,
) -> str:
    inside = [item for item in bad if start - EPS <= item.epoch <= end + EPS]
    blocking = [item for item in inside if item.blocking]
    if any(item.reason == REASON_CONFLICT for item in blocking):
        return REASON_CONFLICT
    if blocking:
        return REASON_BAD_EVIDENCE
    if inside:
        return REASON_GAP
    if per_point_samples is not None and stale_seconds is not None:
        # Explain an unknown span that opens because a held sample expired.
        for samples in per_point_samples.values():
            earlier = [sample for sample in samples if sample.epoch <= start + EPS]
            if not earlier:
                continue
            last = earlier[-1]
            if start - last.epoch >= stale_seconds - EPS:
                later = [sample for sample in samples if sample.epoch > start + EPS]
                if not later:
                    return REASON_STALE
    return REASON_GAP


def _state_samples_at(
    rule: InterlockRule,
    per_point_samples: dict[str, list[Sample]],
    per_point_bad_epochs: dict[str, list[float]],
    epoch: float,
) -> tuple[Sample | None, Sample | None]:
    states = []
    for point in rule.points:
        states.append(_point_state(
            per_point_samples[point.point_key],
            per_point_bad_epochs[point.point_key],
            epoch,
            rule.max_stale_seconds,
        ))
    return states[0], states[1]


def _extreme(point: InterlockPoint, values: list[float]) -> float:
    return max(values) if point.direction == DIRECTION_ABOVE else min(values)


def _hold_expiring_at(
    samples: list[Sample], stale_seconds: float, epoch: float
) -> Sample | None:
    """Return the sample whose hold expires exactly at ``epoch``, if any."""
    times = [sample.epoch for sample in samples]
    index = bisect.bisect_right(times, epoch + EPS) - 1
    if index < 0:
        return None
    last = samples[index]
    expires = last.epoch + stale_seconds
    if index + 1 < len(samples):
        expires = min(expires, samples[index + 1].epoch)
    if abs(expires - epoch) <= EPS:
        return last
    return None


def _contributing_samples(
    samples: list[Sample],
    bad_epochs: list[float],
    start: float,
    end: float,
    stale_seconds: float,
) -> list[Sample]:
    """Samples whose hold overlaps the true interval ``[start, end)``."""
    contributing: list[Sample] = []
    for index, sample in enumerate(samples):
        if sample.epoch > end + EPS:
            break
        hold_end = min(sample.epoch + stale_seconds, end)
        if index + 1 < len(samples):
            hold_end = min(hold_end, samples[index + 1].epoch)
        bad_index = bisect.bisect_right(bad_epochs, sample.epoch + EPS)
        if bad_index < len(bad_epochs):
            hold_end = min(hold_end, bad_epochs[bad_index])
        if sample.epoch <= start + EPS and hold_end > start + EPS:
            contributing.append(sample)
        elif sample.epoch > start - EPS and sample.epoch < end - EPS:
            contributing.append(sample)
    return contributing


def _event_peaks(
    rule: InterlockRule,
    contributing: dict[str, list[Sample]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, point in enumerate(rule.points, start=1):
        group = contributing[point.point_key]
        if not group:
            result[f"peak_{'ab'[index - 1]}"] = 0.0
            result[f"peak_{'ab'[index - 1]}_ts"] = ""
            continue
        best = _extreme(point, [sample.value for sample in group])
        chosen = next(
            sample for sample in sorted(
                group, key=lambda item: (item.epoch, item.line_no)
            )
            if sample.value == best
        )
        result[f"peak_{'ab'[index - 1]}"] = float(best)
        result[f"peak_{'ab'[index - 1]}_ts"] = chosen.ts
    return result


def _interruption(
    rule: InterlockRule,
    per_point_samples: dict[str, list[Sample]],
    per_point_bad_epochs: dict[str, list[float]],
    bad: list[BadEvidence],
    end: float,
    window_end: float,
) -> tuple[str, list[int]]:
    if abs(end - window_end) <= EPS:
        return REASON_END, []
    lines: set[int] = set()
    reasons: list[str] = []
    for point in rule.points:
        sample = _point_state(
            per_point_samples[point.point_key],
            per_point_bad_epochs[point.point_key],
            end,
            rule.max_stale_seconds,
        )
        if sample is None:
            instant = [
                item for item in bad
                if abs(item.epoch - end) <= EPS and item.blocking
            ]
            if instant:
                conflicting = any(
                    item.reason == REASON_CONFLICT for item in instant
                )
                reasons.append(REASON_CONFLICT if conflicting else REASON_BAD_EVIDENCE)
                lines.update(item.line_no for item in instant)
            else:
                reasons.append(REASON_STALE)
                earlier = [
                    candidate for candidate in per_point_samples[point.point_key]
                    if candidate.epoch <= end + EPS
                ]
                if earlier:
                    lines.add(earlier[-1].line_no)
        elif not point.is_triggered(sample.value):
            reasons.append(REASON_EXPLICIT_NORMAL)
            lines.add(sample.line_no)
    if REASON_EXPLICIT_NORMAL in reasons:
        return REASON_EXPLICIT_NORMAL, sorted(lines)
    if REASON_CONFLICT in reasons:
        return REASON_CONFLICT, sorted(lines)
    if REASON_BAD_EVIDENCE in reasons:
        return REASON_BAD_EVIDENCE, sorted(lines)
    if REASON_STALE in reasons:
        return REASON_STALE, sorted(lines)
    return REASON_GAP, sorted(lines)


def _ts_for_epoch(epoch: float, samples: list[Sample], bad: list[BadEvidence]) -> str:
    candidates: list[tuple[int, str]] = []
    candidates.extend((sample.line_no, sample.ts) for sample in samples
                      if abs(sample.epoch - epoch) <= EPS)
    candidates.extend((item.line_no, item.ts) for item in bad
                      if abs(item.epoch - epoch) <= EPS)
    if candidates:
        candidates.sort()
        return candidates[0][1]
    return format_epoch(epoch)


def evaluate_rule(
    rule: InterlockRule,
    samples_by_key: dict[str, Iterable[Sample]],
    bad_by_key: dict[str, Iterable[BadEvidence]],
) -> RuleEvaluation:
    """Evaluate one rule against already-collected timeline evidence."""
    per_point_samples: dict[str, list[Sample]] = {}
    per_point_bad: dict[str, list[BadEvidence]] = {}
    all_samples: list[Sample] = []
    all_bad: list[BadEvidence] = []
    for point in rule.points:
        merged_samples, merged_bad = _merge_evidence(
            samples_by_key.get(point.point_key, ()),
            bad_by_key.get(point.point_key, ()),
        )
        per_point_samples[point.point_key] = merged_samples
        per_point_bad[point.point_key] = merged_bad
        all_samples.extend(merged_samples)
        all_bad.extend(merged_bad)
    all_samples.sort(key=lambda sample: (sample.epoch, sample.line_no))
    all_bad.sort(key=lambda item: (item.epoch, item.line_no, item.reason))

    epochs = [sample.epoch for sample in all_samples] + [
        item.epoch for item in all_bad
    ]
    if not epochs:
        return RuleEvaluation(rule=rule)
    start, end = min(epochs), max(epochs)
    evaluation = RuleEvaluation(
        rule=rule,
        window_start_ts=_ts_for_epoch(start, all_samples, all_bad),
        window_end_ts=_ts_for_epoch(end, all_samples, all_bad),
        window_seconds=max(0.0, end - start),
    )
    if end - start <= EPS:
        return evaluation

    bad_epochs = {
        key: sorted({item.epoch for item in group if item.blocking})
        for key, group in per_point_bad.items()
    }

    if rule.sampling == SAMPLING_STRICT_SIMULTANEOUS:
        _evaluate_strict(rule, evaluation, per_point_samples, bad_epochs,
                         all_samples, all_bad, start, end)
        return evaluation

    _evaluate_bounded(rule, evaluation, per_point_samples, bad_epochs,
                      all_samples, all_bad, start, end)
    return evaluation


def _state_interval(
    state: str,
    start: float,
    end: float,
    all_samples: list[Sample],
    all_bad: list[BadEvidence],
    extra_lines: Iterable[int] = (),
    reason: str | None = None,
    per_point_samples: dict[str, list[Sample]] | None = None,
    stale_seconds: float | None = None,
) -> dict[str, Any]:
    lines = _interval_evidence_lines(start, end, all_samples, all_bad, extra_lines)
    if reason is None:
        if state == STATE_TRUE:
            reason = REASON_BOTH_TRUE
        elif state == STATE_FALSE:
            reason = REASON_EXPLICIT_NORMAL
        else:
            reason = _unknown_reason(
                start, end, all_bad, per_point_samples, stale_seconds
            )
    return {
        "state": state,
        "start_epoch": start,
        "end_epoch": end,
        "start_ts": _ts_for_epoch(start, all_samples, all_bad),
        "end_ts": _ts_for_epoch(end, all_samples, all_bad),
        "duration_seconds": round(max(0.0, end - start), 6),
        "reason": reason,
        "line_numbers": lines,
        "sample_count": sum(
            1 for sample in all_samples
            if start - EPS <= sample.epoch <= end + EPS
        ),
    }


def _append_interval(
    intervals: list[dict[str, Any]], interval: dict[str, Any]
) -> None:
    if (intervals and intervals[-1]["state"] == interval["state"]
            and abs(intervals[-1]["end_epoch"] - interval["start_epoch"]) <= EPS):
        previous = intervals[-1]
        previous["end_epoch"] = interval["end_epoch"]
        previous["end_ts"] = interval["end_ts"]
        previous["duration_seconds"] = round(
            max(0.0, previous["end_epoch"] - previous["start_epoch"]), 6
        )
        previous["line_numbers"] = sorted(set(previous["line_numbers"])
                                          | set(interval["line_numbers"]))
        previous["sample_count"] += interval["sample_count"]
        if previous["reason"] != interval["reason"]:
            previous["reason"] = "+".join(sorted({
                *previous["reason"].split("+"), *interval["reason"].split("+")
            }))
        return
    intervals.append(interval)


def _evaluate_bounded(
    rule: InterlockRule,
    evaluation: RuleEvaluation,
    per_point_samples: dict[str, list[Sample]],
    per_point_bad_epochs: dict[str, list[float]],
    all_samples: list[Sample],
    all_bad: list[BadEvidence],
    start: float,
    end: float,
) -> None:
    boundaries = _boundaries(all_samples, all_bad, rule.max_stale_seconds, start, end)
    intervals: list[dict[str, Any]] = []
    for left, right in zip(boundaries, boundaries[1:]):
        if right - left <= EPS:
            continue
        states = _state_samples_at(
            rule, per_point_samples, per_point_bad_epochs, left
        )
        state = _combined_state(rule.points, states)
        extra_lines = [
            sample.line_no for sample in states if sample is not None
        ]
        if state == STATE_UNKNOWN:
            # Attribute an unknown span that opens at stale expiry to the
            # samples whose holds just expired, and any conflicting evidence.
            for point in rule.points:
                expiring = _hold_expiring_at(
                    per_point_samples[point.point_key],
                    rule.max_stale_seconds, left,
                )
                if expiring is not None:
                    extra_lines.append(expiring.line_no)
            for evidence in all_bad:
                if abs(evidence.epoch - left) <= EPS:
                    extra_lines.append(evidence.line_no)
        interval = _state_interval(
            state, left, right, all_samples, all_bad, extra_lines,
            per_point_samples=per_point_samples,
            stale_seconds=rule.max_stale_seconds,
        )
        _append_interval(intervals, interval)

    for interval in intervals:
        if interval["state"] != STATE_TRUE:
            continue
        if interval["duration_seconds"] + EPS < rule.min_duration_seconds:
            continue
        reason, interruption_lines = _interruption(
            rule, per_point_samples, per_point_bad_epochs, all_bad,
            interval["end_epoch"], end,
        )
        contributing = {
            point.point_key: _contributing_samples(
                per_point_samples[point.point_key],
                per_point_bad_epochs[point.point_key],
                interval["start_epoch"],
                interval["end_epoch"],
                rule.max_stale_seconds,
            )
            for point in rule.points
        }
        peaks = _event_peaks(rule, contributing)
        lines = sorted({
            *interval["line_numbers"],
            *(sample.line_no for group in contributing.values() for sample in group),
        })
        event = {
            "rule_id": rule.rule_id,
            "rule_version": rule.version,
            "device_id": rule.device_id,
            "sampling": rule.sampling,
            "point_a": rule.points[0].point_key,
            "point_b": rule.points[1].point_key,
            "start_ts": interval["start_ts"],
            "end_ts": interval["end_ts"],
            "duration_seconds": interval["duration_seconds"],
            "peak_a": peaks["peak_a"],
            "peak_b": peaks["peak_b"],
            "peak_a_ts": peaks["peak_a_ts"],
            "peak_b_ts": peaks["peak_b_ts"],
            "line_numbers": lines,
            "interruption_reason": reason,
            "interruption_lines": interruption_lines,
        }
        evaluation.events.append(event)
    evaluation.intervals = intervals


def _evaluate_strict(
    rule: InterlockRule,
    evaluation: RuleEvaluation,
    per_point_samples: dict[str, list[Sample]],
    per_point_bad_epochs: dict[str, list[float]],
    all_samples: list[Sample],
    all_bad: list[BadEvidence],
    start: float,
    end: float,
) -> None:
    sample_epochs = {
        key: {sample.epoch for sample in group}
        for key, group in per_point_samples.items()
    }
    bad_epochs = {
        key: set(group) for key, group in per_point_bad_epochs.items()
    }
    shared = sorted(
        sample_epochs[rule.points[0].point_key]
        & sample_epochs[rule.points[1].point_key]
    )
    shared = [
        epoch for epoch in shared
        if not any(epoch in bad_epochs[point.point_key] for point in rule.points)
    ]

    def sample_at(point_key: str, epoch: float) -> Sample:
        return next(
            sample for sample in per_point_samples[point_key]
            if sample.epoch == epoch
        )

    intervals: list[dict[str, Any]] = []
    cursor = start
    for epoch in shared:
        if epoch - cursor > EPS:
            _append_interval(intervals, _state_interval(
                STATE_UNKNOWN, cursor, epoch, all_samples, all_bad,
                reason=REASON_NO_SIMULTANEOUS,
            ))
        states = tuple(
            sample_at(point.point_key, epoch) for point in rule.points
        )
        state = _combined_state(rule.points, states)  # type: ignore[arg-type]
        _append_interval(intervals, _state_interval(
            state, epoch, epoch, all_samples, all_bad,
            [sample.line_no for sample in states], reason=REASON_SIMULTANEOUS,
        ))
        cursor = epoch
    if end - cursor > EPS:
        _append_interval(intervals, _state_interval(
            STATE_UNKNOWN, cursor, end, all_samples, all_bad,
            reason=REASON_NO_SIMULTANEOUS,
        ))
    evaluation.intervals = intervals
    # No continuous coverage can be established from discrete simultaneous
    # instants, so strict mode legitimately emits no sustained event.


def event_key(batch: dict[str, Any], rule: InterlockRule, event: dict[str, Any]) -> str:
    payload = "|".join([
        str(batch["replay_sha256"]),
        str(batch["contract_sha256"]),
        rule.rule_id,
        str(rule.version),
        rule.sampling,
        event["start_ts"],
        event["end_ts"],
    ])
    return "ev_" + sha256_text(payload)[:32]


def collect_rule_evidence(
    store: Store, batch_id: str, rule: InterlockRule
) -> tuple[dict[str, list[Sample]], dict[str, list[BadEvidence]]]:
    """Read trusted readings and bad/conflicting frames for a rule's points."""
    samples: dict[str, list[Sample]] = {}
    bad: dict[str, list[BadEvidence]] = {}
    for point in rule.points:
        key = point.point_key
        samples[key] = []
        bad[key] = []
        for row in store.conn.execute(
            "SELECT ts, line_no, engineering_value FROM readings "
            "WHERE batch_id = ? AND device_id = ? AND point_key = ? "
            "AND quality_code = 'good' ORDER BY line_no",
            (batch_id, rule.device_id, key),
        ):
            epoch = parse_timestamp(row["ts"])
            if epoch is None:
                continue
            samples[key].append(Sample(
                epoch=epoch, ts=row["ts"], line_no=int(row["line_no"]),
                value=float(row["engineering_value"]),
            ))
        for row in store.conn.execute(
            "SELECT f.line_no, f.ts, f.quality_code, f.issue_code "
            "FROM frames f WHERE f.batch_id = ? AND f.device_id = ? "
            "AND f.point_key = ? AND f.quality_code <> 'good' "
            "AND (f.quality_code <> 'duplicate' "
            "     OR f.issue_code = 'contradictory_value') "
            "ORDER BY f.line_no",
            (batch_id, rule.device_id, key),
        ):
            epoch = parse_timestamp(row["ts"])
            if epoch is None:
                continue
            reason = row["issue_code"] or row["quality_code"] or REASON_BAD_EVIDENCE
            # An empty/absent response is not a bad value: it is the absence
            # of a new sample, so a still-valid hold continues past it.
            blocks_hold = row["quality_code"] != "missing_response"
            bad[key].append(BadEvidence(
                epoch=epoch, ts=row["ts"], line_no=int(row["line_no"]),
                reason=reason, blocks_hold=blocks_hold,
            ))
    return samples, bad


def evaluate_batch(
    store: Store,
    batch_id: str,
    contract: ContractSet,
    *,
    persist: bool = True,
) -> dict[str, RuleEvaluation]:
    """Evaluate every interlock rule of a batch and optionally persist it.

    Persistence replaces the batch's derived rows inside one transaction, so
    repeated invocation (re-runs, resumed tails, explicit recomputation) is
    idempotent and never inserts duplicate events.
    """
    batch = store.get_batch(batch_id)
    if batch is None:
        raise ValueError(f"batch {batch_id} does not exist")
    evaluations: dict[str, RuleEvaluation] = {}
    interval_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    for rule in contract.interlocks:
        samples, bad = collect_rule_evidence(store, batch_id, rule)
        evaluation = evaluate_rule(rule, samples, bad)
        evaluations[rule.rule_id] = evaluation
        for interval in evaluation.intervals:
            interval_rows.append({
                "batch_id": batch_id,
                "rule_id": rule.rule_id,
                "rule_version": rule.version,
                "device_id": rule.device_id,
                "state": interval["state"],
                "start_ts": interval["start_ts"],
                "end_ts": interval["end_ts"],
                "start_epoch": interval["start_epoch"],
                "end_epoch": interval["end_epoch"],
                "duration_seconds": interval["duration_seconds"],
                "reason": interval["reason"],
                "line_numbers": ",".join(
                    str(number) for number in interval["line_numbers"]
                ),
                "sample_count": interval["sample_count"],
            })
        for event in evaluation.events:
            event_rows.append({
                "batch_id": batch_id,
                "event_key": event_key(batch, rule, event),
                "rule_id": rule.rule_id,
                "rule_version": rule.version,
                "device_id": rule.device_id,
                "point_a": event["point_a"],
                "point_b": event["point_b"],
                "start_ts": event["start_ts"],
                "end_ts": event["end_ts"],
                "duration_seconds": event["duration_seconds"],
                "peak_a": event["peak_a"],
                "peak_b": event["peak_b"],
                "peak_a_ts": event["peak_a_ts"],
                "peak_b_ts": event["peak_b_ts"],
                "line_numbers": ",".join(
                    str(number) for number in event["line_numbers"]
                ),
                "interruption_reason": event["interruption_reason"],
                "interruption_lines": ",".join(
                    str(number) for number in event["interruption_lines"]
                ),
                "sampling": event["sampling"],
            })
    if persist:
        store.replace_interlocks(batch_id, interval_rows, event_rows)
    return evaluations
