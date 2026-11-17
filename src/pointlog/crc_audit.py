"""Exhaustive bounded-fault audit for explicitly selected good responses.

CRC 合格只说明该帧通过既定校验，并不等于所有传输位错都能被 CRC 发现。本模块在
用户明确授权的链路故障模型下（``single_bit`` 单 bit 故障，或 ``multi_bit_4``
最多 4 bit 联合故障），对完整批次中明确选定的 good 响应穷举有限的位翻转组合
空间：从响应帧全部字节（含 CRC）选择恰好 k 个不同 bit 翻转，对每种组合重新执行
CRC 校验、响应结构解析、请求 slave/function/count 匹配、契约解码与范围/枚举
质量检查，并精确统计去向（CRC 拦截、结构拦截、质量拦截、仍为 good 但工程值变化
的静默变异、仍为 good 且工程值不变）。每种组合只计入一个类别，分类之和等于
组合数 C(总 bit 数, k) 之和。

这是假设性审计：它不修改任何 quality_code，不计入实际坏帧，也不产生待重采点。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import comb
from typing import Any, Sequence

from . import codec
from .contract import ContractSet, PointContract
from .crc import crc_bit_syndromes, flip_bits, verify_crc
from .modbus import (
    FrameParseError,
    RequestFrame,
    ResponseFrame,
    hex_to_bytes,
    match_response,
    parse_request,
    parse_response,
)
from .quality import GOOD, QualityFlags, determine_quality
from .store import Store
from .util import frame_summary, normalize_number

# Normal responses in this repository carry 1-2 registers (int16/int32/float32
# contracts), i.e. at most slave+function+byte_count+4 data bytes+CRC = 9 bytes.
# Larger frames are rejected explicitly instead of being sampled.
MAX_RESPONSE_BYTES = 9
MAX_AUDIT_FRAMES = 8
# Response summaries in the report are truncated so the full raw frame is
# never disclosed by the audit output.
SUMMARY_LIMIT = 12

CRC_INTERCEPTED = "crc_intercepted"
STRUCTURE_INTERCEPTED = "structure_intercepted"
QUALITY_INTERCEPTED = "quality_intercepted"
SILENT = "silent"
BENIGN = "benign"
CATEGORIES = (
    CRC_INTERCEPTED,
    STRUCTURE_INTERCEPTED,
    QUALITY_INTERCEPTED,
    SILENT,
    BENIGN,
)

MODEL_SINGLE_BIT = "single_bit"
MODEL_MULTI_BIT_4 = "multi_bit_4"
FAULT_MODELS: dict[str, tuple[str, tuple[int, ...]]] = {
    MODEL_SINGLE_BIT: ("单 bit 故障", (1,)),
    MODEL_MULTI_BIT_4: ("最多 4 bit 联合故障", (1, 2, 3, 4)),
}


class AuditError(ValueError):
    """An audit input problem with a stable machine-readable code."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _model(model: str) -> tuple[str, tuple[int, ...]]:
    try:
        return FAULT_MODELS[model]
    except KeyError:
        known = ", ".join(sorted(FAULT_MODELS))
        raise AuditError(
            "unknown_fault_model", f"fault model {model!r} is not one of: {known}"
        ) from None


def _quality_for(check: codec.ValueCheck) -> str:
    return determine_quality(QualityFlags(
        overflow=check.range_code == "overflow",
        under_range=check.range_code == "under_range",
        not_in_enum=not check.in_enum,
    ))


def _check_point(data: bytes, point: PointContract) -> codec.ValueCheck:
    return codec.check_value(
        data,
        point.data_type,
        point.byte_order,
        point.scale,
        point.offset,
        point.range_min,
        point.range_max,
        point.enum,
    )


@dataclass(frozen=True)
class Baseline:
    """A validated good request/response pair used as the audit baseline."""

    request: RequestFrame
    response: ResponseFrame
    point: PointContract
    check: codec.ValueCheck


def prepare_baseline(
    request_frame: bytes, response_frame: bytes, contract: ContractSet
) -> Baseline:
    """Validate one selected response and return its trustworthy baseline.

    Illegal, mismatched, exception or non-good original frames are rejected:
    they have no trustworthy baseline and cannot be audited as good readings.
    """
    try:
        request = parse_request(request_frame)
    except FrameParseError as exc:
        raise AuditError("bad_request_frame", f"request: {exc.detail}") from exc
    if not request.crc_ok:
        raise AuditError("bad_crc_request", "request CRC check failed")
    point = contract.find(
        request.slave_id, request.function, request.start, request.count
    )
    if point is None:
        raise AuditError(
            "no_contract",
            f"no contract for slave={request.slave_id} function={request.function} "
            f"start={request.start} count={request.count}",
        )
    if len(response_frame) > MAX_RESPONSE_BYTES:
        raise AuditError(
            "frame_too_large",
            f"response has {len(response_frame)} bytes; the exact audit supports "
            f"at most {MAX_RESPONSE_BYTES} bytes (1-2 register contracts)",
        )
    try:
        response = parse_response(response_frame)
    except FrameParseError as exc:
        raise AuditError("bad_response_frame", f"response: {exc.detail}") from exc
    if not response.crc_ok:
        raise AuditError("bad_crc_response", "response CRC check failed")
    try:
        match_response(request, response, point.count * 2)
    except FrameParseError as exc:
        raise AuditError(exc.code, exc.detail) from exc
    if response.is_exception:
        raise AuditError(
            "exception_response",
            "exception responses have no trustworthy good baseline",
        )
    try:
        check = _check_point(response.data, point)
    except codec.CodecError as exc:
        raise AuditError("decode_error", f"decode failed: {exc.detail}") from exc
    quality = _quality_for(check)
    if quality != GOOD:
        raise AuditError(
            "quality_not_good",
            f"baseline response quality is {quality}, not good; "
            "only good responses have a trustworthy baseline",
        )
    return Baseline(request=request, response=response, point=point, check=check)


def _classify_crc_passing(
    baseline: Baseline, mutated_frame: bytes
) -> tuple[str, codec.ValueCheck | None]:
    """Classify a mutated frame whose CRC is known to pass."""
    try:
        response = parse_response(mutated_frame)
    except FrameParseError:
        return STRUCTURE_INTERCEPTED, None
    try:
        match_response(baseline.request, response, baseline.point.count * 2)
    except FrameParseError:
        return STRUCTURE_INTERCEPTED, None
    if response.is_exception:
        return QUALITY_INTERCEPTED, None
    try:
        check = _check_point(response.data, baseline.point)
    except codec.CodecError:
        return QUALITY_INTERCEPTED, None
    if _quality_for(check) != GOOD:
        return QUALITY_INTERCEPTED, None
    # A mutation that leaves the engineering value unchanged (for example one
    # that only rewrites the CRC field, or a -0.0/+0.0 float pattern) is never
    # a silent reading mutation.
    if check.engineering != baseline.check.engineering:
        return SILENT, check
    return BENIGN, check


def classify_mutation(
    baseline: Baseline, mutated_frame: bytes
) -> tuple[str, codec.ValueCheck | None]:
    """Classify one mutated response frame against *baseline*.

    Returns ``(category, value_check)``; the value check is only present for
    mutations that stay good. Categories are mutually exclusive and evaluated
    in pipeline order: CRC, structure, quality, silent, benign.
    """
    if not verify_crc(mutated_frame):
        return CRC_INTERCEPTED, None
    return _classify_crc_passing(baseline, mutated_frame)


@dataclass(frozen=True)
class SilentCase:
    """The minimal silent mutation found for one response."""

    bits: tuple[int, ...]
    original_summary: str
    mutated_summary: str
    original_raw: int | float
    mutated_raw: int | float
    original_engineering: float
    mutated_engineering: float
    device_id: str
    point_key: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bits": list(self.bits),
            "original_response_summary": self.original_summary,
            "mutated_response_summary": self.mutated_summary,
            "original_raw_value": normalize_number(self.original_raw),
            "mutated_raw_value": normalize_number(self.mutated_raw),
            "original_engineering_value": normalize_number(self.original_engineering),
            "mutated_engineering_value": normalize_number(self.mutated_engineering),
            "device_id": self.device_id,
            "point_key": self.point_key,
        }


@dataclass(frozen=True)
class ResponseAudit:
    """Exact exhaustive audit result for one selected good response."""

    device_id: str
    point_key: str
    frame_bits: int
    combinations: int
    crc_intercepted: int
    structure_intercepted: int
    quality_intercepted: int
    silent: int
    benign: int
    response_summary: str
    baseline_raw: int | float
    baseline_engineering: float
    baseline_label: str | None
    minimal_silent: SilentCase | None

    def category_counts(self) -> dict[str, int]:
        return {
            CRC_INTERCEPTED: self.crc_intercepted,
            STRUCTURE_INTERCEPTED: self.structure_intercepted,
            QUALITY_INTERCEPTED: self.quality_intercepted,
            SILENT: self.silent,
            BENIGN: self.benign,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "point_key": self.point_key,
            "frame_bits": self.frame_bits,
            "response_summary": self.response_summary,
            "baseline": {
                "raw_value": normalize_number(self.baseline_raw),
                "engineering_value": normalize_number(self.baseline_engineering),
                "enum_label": self.baseline_label,
            },
            "combinations": self.combinations,
            **self.category_counts(),
            "minimal_silent": (
                self.minimal_silent.as_dict() if self.minimal_silent else None
            ),
        }


def audit_response(
    request_frame: bytes,
    response_frame: bytes,
    contract: ContractSet,
    model: str = MODEL_MULTI_BIT_4,
) -> ResponseAudit:
    """Exhaustively audit one good response under the given fault model.

    Every combination of exactly k distinct flipped bits (k from the model)
    over the whole response frame, CRC bytes included, is re-checked and
    counted in exactly one category.
    """
    _, ks = _model(model)  # the model label is reported by the batch audit
    baseline = prepare_baseline(request_frame, response_frame, contract)
    frame = baseline.response.raw
    frame_bits = len(frame) * 8
    syndromes = crc_bit_syndromes(frame)
    counts = dict.fromkeys(CATEGORIES, 0)
    minimal: SilentCase | None = None
    for k in ks:
        for combo in combinations(range(frame_bits), k):
            syndrome = 0
            for bit in combo:
                syndrome ^= syndromes[bit]
            if syndrome:
                counts[CRC_INTERCEPTED] += 1
                continue
            mutated = flip_bits(frame, combo)
            category, check = _classify_crc_passing(baseline, mutated)
            counts[category] += 1
            # combinations() yields lexicographic order and k ascends, so the
            # first silent case found is the minimal counterexample.
            if category == SILENT and minimal is None:
                minimal = SilentCase(
                    bits=combo,
                    original_summary=frame_summary(frame.hex(), SUMMARY_LIMIT),
                    mutated_summary=frame_summary(mutated.hex(), SUMMARY_LIMIT),
                    original_raw=baseline.check.raw,
                    mutated_raw=check.raw if check else baseline.check.raw,
                    original_engineering=baseline.check.engineering,
                    mutated_engineering=(
                        check.engineering if check else baseline.check.engineering
                    ),
                    device_id=baseline.point.device_id,
                    point_key=baseline.point.point_key,
                )
    total = sum(counts.values())
    expected = sum(comb(frame_bits, k) for k in ks)
    if total != expected:  # pragma: no cover - defensive invariant
        raise RuntimeError(
            f"audit counted {total} combinations, expected {expected}"
        )
    return ResponseAudit(
        device_id=baseline.point.device_id,
        point_key=baseline.point.point_key,
        frame_bits=frame_bits,
        combinations=total,
        crc_intercepted=counts[CRC_INTERCEPTED],
        structure_intercepted=counts[STRUCTURE_INTERCEPTED],
        quality_intercepted=counts[QUALITY_INTERCEPTED],
        silent=counts[SILENT],
        benign=counts[BENIGN],
        response_summary=frame_summary(frame.hex(), SUMMARY_LIMIT),
        baseline_raw=baseline.check.raw,
        baseline_engineering=baseline.check.engineering,
        baseline_label=baseline.check.label,
        minimal_silent=minimal,
    )


@dataclass(frozen=True)
class BatchAudit:
    """Audit of an explicit selection of good responses under one fault model."""

    model: str
    model_label: str
    ks: tuple[int, ...]
    responses: tuple[ResponseAudit, ...]

    def as_dict(self) -> dict[str, Any]:
        totals = {category: 0 for category in CATEGORIES}
        total_combinations = 0
        for response in self.responses:
            total_combinations += response.combinations
            for category, count in response.category_counts().items():
                totals[category] += count
        silent_total = totals[SILENT]
        frame_count = len(self.responses)
        if silent_total:
            conclusion = (
                f"在「{self.model_label}」模型下，{frame_count} 个选定响应的 "
                f"{total_combinations} 种位翻转组合中存在 {silent_total} 种静默读数"
                "变异（CRC 合格、结构与质量判定仍为 good，但工程值改变）。"
            )
        else:
            conclusion = (
                f"在「{self.model_label}」模型下，{frame_count} 个选定响应的 "
                f"{total_combinations} 种位翻转组合均被 CRC/结构/质量检查拦截或保持"
                "原工程值，未发现静默读数变异。"
            )
        return {
            "schema_version": 1,
            "hypothetical": True,
            "counts_toward_quality": False,
            "note": "假设性故障空间审计：不修改 quality_code，不计入实际坏帧、"
                    "质量分布或待重采清单。",
            "fault_model": self.model,
            "fault_model_label": self.model_label,
            "ks": list(self.ks),
            "frame_count": frame_count,
            "max_frames": MAX_AUDIT_FRAMES,
            "totals": {"combinations": total_combinations, **totals},
            "risk": {
                "silent_mutations": silent_total,
                "silent_possible": silent_total > 0,
                "conclusion": conclusion,
            },
            "responses": [response.as_dict() for response in self.responses],
        }


def audit_frames(
    selections: Sequence[tuple[bytes, bytes]],
    contract: ContractSet,
    model: str = MODEL_MULTI_BIT_4,
) -> BatchAudit:
    """Audit an explicit selection of (request, response) frame pairs.

    At most MAX_AUDIT_FRAMES responses per audit run. The result is
    deterministic and independent of the selection order.
    """
    label, ks = _model(model)
    if not selections:
        raise AuditError("empty_selection", "select at least one response to audit")
    if len(selections) > MAX_AUDIT_FRAMES:
        raise AuditError(
            "too_many_frames",
            f"one audit covers at most {MAX_AUDIT_FRAMES} responses, "
            f"got {len(selections)}",
        )
    audits = [
        audit_response(request_frame, response_frame, contract, model)
        for request_frame, response_frame in selections
    ]
    audits.sort(key=lambda audit: (
        audit.device_id,
        audit.point_key,
        audit.frame_bits,
        str(audit.baseline_raw),
    ))
    return BatchAudit(model=model, model_label=label, ks=ks, responses=tuple(audits))


def audit_store_frames(
    store: Store,
    batch_id: str,
    contract: ContractSet,
    line_numbers: Sequence[int],
    model: str = MODEL_MULTI_BIT_4,
) -> BatchAudit:
    """Audit explicitly selected good response frames of a completed batch."""
    batch = store.get_batch(batch_id)
    if batch is None:
        raise AuditError("batch_missing", f"batch {batch_id} does not exist")
    if batch["status"] != "completed":
        raise AuditError(
            "batch_not_complete",
            f"batch {batch_id} status is {batch['status']}, not completed",
        )
    if not line_numbers:
        raise AuditError("empty_selection", "select at least one line to audit")
    selected = sorted(set(line_numbers))
    if len(selected) > MAX_AUDIT_FRAMES:
        raise AuditError(
            "too_many_frames",
            f"one audit covers at most {MAX_AUDIT_FRAMES} responses, "
            f"got {len(selected)} distinct lines",
        )
    selections: list[tuple[bytes, bytes]] = []
    for line_no in selected:
        row = store.conn.execute(
            "SELECT quality_code, contract_matched, request_hex, response_hex "
            "FROM frames WHERE batch_id = ? AND line_no = ?",
            (batch_id, line_no),
        ).fetchone()
        if row is None:
            raise AuditError(
                "frame_missing", f"batch {batch_id} has no frame at line {line_no}"
            )
        if (
            row["quality_code"] != GOOD
            or not row["contract_matched"]
            or not row["response_hex"].strip()
        ):
            raise AuditError(
                "frame_not_auditable",
                f"line {line_no} is not a contract-matched good response "
                f"(quality_code={row['quality_code']})",
            )
        selections.append(
            (hex_to_bytes(row["request_hex"]), hex_to_bytes(row["response_hex"]))
        )
    return audit_frames(selections, contract, model)
