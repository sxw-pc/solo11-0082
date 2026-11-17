"""Tests for the exhaustive bounded-fault CRC audit (pointlog.crc_audit)."""

import json
import math

import pytest

from pointlog import codec
from pointlog.contract import load_contract, parse_contract
from pointlog.crc import append_crc, crc_bit_syndromes, flip_bits, verify_crc
from pointlog.crc_audit import (
    BENIGN,
    CRC_INTERCEPTED,
    MAX_AUDIT_FRAMES,
    MAX_RESPONSE_BYTES,
    QUALITY_INTERCEPTED,
    SILENT,
    STRUCTURE_INTERCEPTED,
    AuditError,
    audit_frames,
    audit_response,
    audit_store_frames,
    classify_mutation,
    prepare_baseline,
)
from pointlog.lineproc import process_line
from pointlog.modbus import (
    build_exception_response,
    build_request,
    build_response,
    corrupt_crc,
)
from pointlog.replay import ReplayLine
from pointlog.report_stats import collect_summary
from pointlog.store import Store

# The mandated synthetic scenario: a two-register int32 response whose CRC
# keeps passing after flipping bits 24, 25, 38 and 40 (bit 0 is the least
# significant bit of byte 0, numbered frame-wide from 0).
MANDATED_CONTRACT_TEXT = """
version: 1
devices:
  - id: gw
    label: Gateway
    points:
      - key: energy
        slave_id: 1
        function: 3
        start: 0
        count: 2
        data_type: int32
        byte_order: ABCD
        scale: 1
        offset: 0
        unit: kWh
        range: [0, 1000000000]
"""
MANDATED_REQUEST = build_request(1, 3, 0, 2)
ORIGINAL_RESPONSE = bytes.fromhex("010304000100022a32")
MUTATED_RESPONSE = bytes.fromhex("010304034101022a32")
MANDATED_BITS = (24, 25, 38, 40)
ORIGINAL_VALUE = 65538
MUTATED_VALUE = 54591746

QUALITY_CONTRACT_TEXT = """
version: 1
devices:
  - id: gw
    label: Gateway
    points:
      - key: temp
        slave_id: 1
        function: 3
        start: 0
        count: 1
        data_type: int16
        byte_order: AB
        scale: 1
        offset: 0
        unit: C
        range: [0, 100]
      - key: mode
        slave_id: 1
        function: 3
        start: 1
        count: 1
        data_type: uint16
        byte_order: AB
        scale: 1
        offset: 0
        range: [0, 10]
        enum: {1: "on", 2: "off"}
      - key: flow
        slave_id: 1
        function: 3
        start: 2
        count: 2
        data_type: float32
        byte_order: ABCD
        scale: 1
        offset: 0
        range: [-1000, 1000]
"""


@pytest.fixture(scope="module")
def mandated_contract():
    return parse_contract(MANDATED_CONTRACT_TEXT)


@pytest.fixture(scope="module")
def quality_contract():
    return parse_contract(QUALITY_CONTRACT_TEXT)


@pytest.fixture(scope="module")
def temp_baseline(quality_contract):
    request = build_request(1, 3, 0, 1)
    response = build_response(1, 3, codec.encode_raw(50, "int16", "AB"))
    return prepare_baseline(request, response, quality_contract)


def test_mandated_scenario_frames_and_values(mandated_contract):
    # Bit numbering: bit i is bit i % 8 of byte i // 8, LSB-first per byte.
    assert flip_bits(ORIGINAL_RESPONSE, MANDATED_BITS) == MUTATED_RESPONSE
    # Both frames pass the Modbus CRC and share slave/function/byte count.
    assert verify_crc(ORIGINAL_RESPONSE)
    assert verify_crc(MUTATED_RESPONSE)
    baseline = prepare_baseline(MANDATED_REQUEST, ORIGINAL_RESPONSE, mandated_contract)
    assert baseline.check.engineering == float(ORIGINAL_VALUE)
    category, check = classify_mutation(baseline, MUTATED_RESPONSE)
    assert category == SILENT
    assert check.engineering == float(MUTATED_VALUE)
    # Both engineering values are inside the declared range [0, 1e9].
    assert 0 <= ORIGINAL_VALUE <= 1_000_000_000
    assert 0 <= MUTATED_VALUE <= 1_000_000_000


def test_mandated_scenario_passes_real_pipeline_as_good(mandated_contract):
    # The mutated frame flows through the actual capture pipeline and still
    # produces a good reading, with a different engineering value.
    store = Store(":memory:")
    try:
        store.create_batch(
            batch_id="b_audit",
            replay_path="replay.jsonl",
            replay_sha256="0" * 64,
            contract_path="points.yaml",
            contract_sha256="1" * 64,
            started_at="2026-01-01T00:00:00Z",
            total_lines=2,
        )
        seen: set[str] = set()
        outcomes = []
        for line_no, response_hex in enumerate(
            (ORIGINAL_RESPONSE.hex(), MUTATED_RESPONSE.hex()), start=1
        ):
            line = ReplayLine(
                line_no=line_no,
                raw_text="",
                ts=f"2026-01-01T00:00:0{line_no}Z",
                request_hex=MANDATED_REQUEST.hex(),
                response_hex=response_hex,
                source="audit-test",
            )
            outcomes.append(process_line(store, "b_audit", mandated_contract, line, seen))
        assert [outcome.frame["quality_code"] for outcome in outcomes] == ["good", "good"]
        assert outcomes[0].reading["engineering_value"] == float(ORIGINAL_VALUE)
        assert outcomes[1].reading["engineering_value"] == float(MUTATED_VALUE)
    finally:
        store.close()


def test_single_bit_model_has_no_silent_mutation(mandated_contract):
    audit = audit_response(
        MANDATED_REQUEST, ORIGINAL_RESPONSE, mandated_contract, model="single_bit"
    )
    frame_bits = len(ORIGINAL_RESPONSE) * 8
    assert audit.combinations == math.comb(frame_bits, 1) == 72
    # CRC-16/MODBUS intercepts every single-bit flip in this frame.
    assert audit.crc_intercepted == 72
    assert audit.structure_intercepted == 0
    assert audit.quality_intercepted == 0
    assert audit.silent == 0
    assert audit.benign == 0
    assert audit.minimal_silent is None


def test_multi_bit_model_finds_mandated_counterexample(mandated_contract):
    audit = audit_response(
        MANDATED_REQUEST, ORIGINAL_RESPONSE, mandated_contract, model="multi_bit_4"
    )
    frame_bits = len(ORIGINAL_RESPONSE) * 8
    expected_total = sum(math.comb(frame_bits, k) for k in (1, 2, 3, 4))
    assert audit.combinations == expected_total == 1_091_058
    # Mutually exclusive categories partition the whole fault space.
    counts = audit.category_counts()
    assert sum(counts.values()) == expected_total
    assert counts == {
        CRC_INTERCEPTED: 1_090_525,
        STRUCTURE_INTERCEPTED: 393,
        QUALITY_INTERCEPTED: 15,
        SILENT: 125,
        BENIGN: 0,
    }
    # The minimal silent counterexample is exactly the mandated 4-bit flip.
    minimal = audit.minimal_silent
    assert minimal is not None
    assert minimal.bits == MANDATED_BITS
    assert minimal.original_engineering == float(ORIGINAL_VALUE)
    assert minimal.mutated_engineering == float(MUTATED_VALUE)
    assert minimal.original_raw == ORIGINAL_VALUE
    assert minimal.mutated_raw == MUTATED_VALUE
    assert (minimal.device_id, minimal.point_key) == ("gw", "energy")
    # Summaries are truncated excerpts, not the full frames.
    assert minimal.original_summary == "010304000100...(18 hex chars)"
    assert minimal.mutated_summary == "010304034101...(18 hex chars)"


def test_category_sum_matches_denominator_for_seven_byte_frame(quality_contract):
    request = build_request(1, 3, 0, 1)
    response = build_response(1, 3, codec.encode_raw(50, "int16", "AB"))
    audit = audit_response(request, response, quality_contract, model="multi_bit_4")
    frame_bits = len(response) * 8
    assert frame_bits == 56
    expected_total = sum(math.comb(frame_bits, k) for k in (1, 2, 3, 4))
    assert audit.combinations == expected_total == 396_606
    counts = audit.category_counts()
    assert sum(counts.values()) == expected_total
    assert counts == {
        CRC_INTERCEPTED: 396_370,
        STRUCTURE_INTERCEPTED: 212,
        QUALITY_INTERCEPTED: 18,
        SILENT: 6,
        BENIGN: 0,
    }
    assert audit.minimal_silent is not None
    assert len(audit.minimal_silent.bits) == 4


def test_crc_byte_flips_are_crc_intercepted(temp_baseline):
    frame = temp_baseline.response.raw
    assert classify_mutation(temp_baseline, corrupt_crc(frame))[0] == CRC_INTERCEPTED
    payload_bits = (len(frame) - 2) * 8
    for bit in range(payload_bits, len(frame) * 8):
        mutated = flip_bits(frame, (bit,))
        assert classify_mutation(temp_baseline, mutated)[0] == CRC_INTERCEPTED


def test_crc_bit_syndromes_match_direct_verification(temp_baseline):
    frame = temp_baseline.response.raw
    syndromes = crc_bit_syndromes(frame)
    assert len(syndromes) == len(frame) * 8
    # CRC-field syndromes are the individual stored-CRC bit values.
    assert syndromes[-16:] == tuple(1 << bit for bit in range(16))
    # Spot-check random combinations against direct verify_crc calls.
    for combo in ((0,), (5, 40), (1, 2, 3), (10, 20, 30, 40), (8, 9, 50, 55)):
        syndrome = 0
        for bit in combo:
            syndrome ^= syndromes[bit]
        assert verify_crc(flip_bits(frame, combo)) == (syndrome == 0)
    with pytest.raises(ValueError):
        crc_bit_syndromes(corrupt_crc(frame))


def test_structure_interceptions(temp_baseline):
    data = codec.encode_raw(50, "int16", "AB")
    # Wrong slave id, valid CRC.
    assert classify_mutation(temp_baseline, build_response(2, 3, data))[0] == (
        STRUCTURE_INTERCEPTED
    )
    # Wrong function code, valid CRC.
    assert classify_mutation(temp_baseline, build_response(1, 4, data))[0] == (
        STRUCTURE_INTERCEPTED
    )
    # Odd data byte count.
    odd = append_crc(bytes((1, 3, 3, 1, 2, 3)))
    assert classify_mutation(temp_baseline, odd)[0] == STRUCTURE_INTERCEPTED
    # Declared byte count does not match the contracted register count.
    wider = build_response(1, 3, b"\x00\x01\x00\x02")
    assert classify_mutation(temp_baseline, wider)[0] == STRUCTURE_INTERCEPTED


def test_quality_interceptions(temp_baseline, quality_contract):
    # Range checks intercept out-of-range engineering values.
    overflow = build_response(1, 3, codec.encode_raw(200, "int16", "AB"))
    assert classify_mutation(temp_baseline, overflow)[0] == QUALITY_INTERCEPTED
    under = build_response(1, 3, codec.encode_raw(-5, "int16", "AB"))
    assert classify_mutation(temp_baseline, under)[0] == QUALITY_INTERCEPTED
    # Enum checks intercept raw values absent from the mapping.
    request = build_request(1, 3, 1, 1)
    baseline = prepare_baseline(
        request, build_response(1, 3, codec.encode_raw(1, "uint16", "AB")),
        quality_contract,
    )
    assert baseline.check.label == "on"
    off_enum = build_response(1, 3, codec.encode_raw(7, "uint16", "AB"))
    assert classify_mutation(baseline, off_enum)[0] == QUALITY_INTERCEPTED


def test_value_preserving_mutation_is_never_silent(quality_contract, temp_baseline):
    # A mutation that only rewrites CRC bytes fails the CRC check outright.
    frame = temp_baseline.response.raw
    only_crc = flip_bits(frame, ((len(frame) - 2) * 8,))
    assert classify_mutation(temp_baseline, only_crc)[0] == CRC_INTERCEPTED
    # A CRC-valid mutation that keeps the engineering value (+0.0 -> -0.0)
    # is benign, not a silent reading mutation.
    request = build_request(1, 3, 2, 2)
    baseline = prepare_baseline(
        request, build_response(1, 3, codec.encode_raw(0.0, "float32", "ABCD")),
        quality_contract,
    )
    negative_zero = build_response(1, 3, bytes.fromhex("80000000"))
    category, check = classify_mutation(baseline, negative_zero)
    assert category == BENIGN
    assert check.engineering == 0.0
    # A CRC-valid mutation that changes the value is the silent case.
    changed = build_response(1, 3, codec.encode_raw(5.0, "float32", "ABCD"))
    assert classify_mutation(baseline, changed)[0] == SILENT


def test_fault_models_answer_their_own_assumptions(mandated_contract):
    selections = [(MANDATED_REQUEST, ORIGINAL_RESPONSE)]
    single = audit_frames(selections, mandated_contract, model="single_bit").as_dict()
    multi = audit_frames(selections, mandated_contract, model="multi_bit_4").as_dict()
    # Both models are internally consistent but answer different link
    # assumptions, so their risk conclusions and minimal counterexamples differ.
    assert single["fault_model"] == "single_bit"
    assert single["ks"] == [1]
    assert single["risk"]["silent_possible"] is False
    assert single["risk"]["silent_mutations"] == 0
    assert single["responses"][0]["minimal_silent"] is None
    assert multi["fault_model"] == "multi_bit_4"
    assert multi["ks"] == [1, 2, 3, 4]
    assert multi["risk"]["silent_possible"] is True
    assert multi["risk"]["silent_mutations"] == 125
    assert multi["responses"][0]["minimal_silent"]["bits"] == list(MANDATED_BITS)
    assert single["risk"]["conclusion"] != multi["risk"]["conclusion"]
    # The audit is hypothetical and never counts as bad frames or recollects.
    for report in (single, multi):
        assert report["hypothetical"] is True
        assert report["counts_toward_quality"] is False
        assert report["max_frames"] == MAX_AUDIT_FRAMES == 8


@pytest.mark.parametrize(
    ("data_type", "byte_order", "raw"),
    [
        ("int16", "AB", -7),
        ("int16", "BA", 258),
        ("uint16", "BA", 65535),
        ("int32", "CDAB", -123456),
        ("uint32", "BADC", 4000000000),
        ("uint32", "DCBA", 42),
        ("float32", "DCBA", 12.5),
    ],
)
def test_data_types_and_byte_orders_are_audited_exactly(data_type, byte_order, raw):
    contract = parse_contract(
        f"""
version: 1
devices:
  - id: gw
    label: Gateway
    points:
      - key: p
        slave_id: 1
        function: 4
        start: 10
        count: {codec.REGISTER_COUNTS[data_type]}
        data_type: {data_type}
        byte_order: {byte_order}
        scale: 1
        offset: 0
        range: [-5000000000, 5000000000]
"""
    )
    request = build_request(1, 4, 10, codec.REGISTER_COUNTS[data_type])
    response = build_response(1, 4, codec.encode_raw(raw, data_type, byte_order))
    baseline = prepare_baseline(request, response, contract)
    assert baseline.check.raw == pytest.approx(raw)
    audit = audit_response(request, response, contract, model="single_bit")
    frame_bits = len(response) * 8
    assert audit.combinations == frame_bits
    assert audit.crc_intercepted == frame_bits
    assert audit.silent == 0
    assert sum(audit.category_counts().values()) == frame_bits


COMBINED_CONTRACT_TEXT = """
version: 1
devices:
  - id: gw
    label: Gateway
    points:
      - key: energy
        slave_id: 1
        function: 3
        start: 0
        count: 2
        data_type: int32
        byte_order: ABCD
        scale: 1
        offset: 0
        unit: kWh
        range: [0, 1000000000]
      - key: temp
        slave_id: 1
        function: 3
        start: 2
        count: 1
        data_type: int16
        byte_order: AB
        scale: 1
        offset: 0
        unit: C
        range: [0, 100]
"""


@pytest.fixture(scope="module")
def combined_contract():
    return parse_contract(COMBINED_CONTRACT_TEXT)


def test_input_order_stability_and_repeatability(mandated_contract, combined_contract):
    first = (MANDATED_REQUEST, ORIGINAL_RESPONSE)
    second = (
        build_request(1, 3, 2, 1),
        build_response(1, 3, codec.encode_raw(50, "int16", "AB")),
    )
    forward = audit_frames([first, second], combined_contract, model="single_bit")
    backward = audit_frames([second, first], combined_contract, model="single_bit")
    assert forward.as_dict() == backward.as_dict()
    # Repeated runs are byte-stable.
    again = audit_frames([second, first], combined_contract, model="single_bit")
    assert again.as_dict() == backward.as_dict()
    # The heavy multi-bit scan is deterministic too.
    heavy_first = audit_frames([first], mandated_contract, model="multi_bit_4")
    heavy_second = audit_frames([first], mandated_contract, model="multi_bit_4")
    assert heavy_first.as_dict() == heavy_second.as_dict()


def test_frame_larger_than_nine_bytes_is_rejected(quality_contract):
    # A 6-data-byte response (11 bytes) exceeds the exact 1-2 register scope.
    request = build_request(1, 3, 2, 2)
    response = build_response(1, 3, b"\x00" * 6)
    assert len(response) == MAX_RESPONSE_BYTES + 2
    with pytest.raises(AuditError) as excinfo:
        prepare_baseline(request, response, quality_contract)
    assert excinfo.value.code == "frame_too_large"


def test_audit_rejects_untrustworthy_baselines(mandated_contract, quality_contract):
    request = build_request(1, 3, 0, 1)
    good = build_response(1, 3, codec.encode_raw(50, "int16", "AB"))
    with pytest.raises(AuditError) as bad_crc:
        prepare_baseline(request, corrupt_crc(good), quality_contract)
    assert bad_crc.value.code == "bad_crc_response"
    with pytest.raises(AuditError) as bad_req:
        prepare_baseline(corrupt_crc(build_request(1, 3, 0, 1)), good, quality_contract)
    assert bad_req.value.code == "bad_crc_request"
    with pytest.raises(AuditError) as exception:
        prepare_baseline(request, build_exception_response(1, 3, 2), quality_contract)
    assert exception.value.code == "exception_response"
    out_of_range = build_response(1, 3, codec.encode_raw(500, "int16", "AB"))
    with pytest.raises(AuditError) as not_good:
        prepare_baseline(request, out_of_range, quality_contract)
    assert not_good.value.code == "quality_not_good"
    with pytest.raises(AuditError) as no_contract:
        prepare_baseline(build_request(9, 3, 0, 1), good, quality_contract)
    assert no_contract.value.code == "no_contract"
    with pytest.raises(AuditError) as unknown:
        audit_frames([(MANDATED_REQUEST, ORIGINAL_RESPONSE)], mandated_contract,
                     model="burst_16")
    assert unknown.value.code == "unknown_fault_model"


def test_audit_frame_count_limits(mandated_contract):
    selection = (MANDATED_REQUEST, ORIGINAL_RESPONSE)
    with pytest.raises(AuditError) as empty:
        audit_frames([], mandated_contract, model="single_bit")
    assert empty.value.code == "empty_selection"
    with pytest.raises(AuditError) as too_many:
        audit_frames([selection] * (MAX_AUDIT_FRAMES + 1), mandated_contract,
                     model="single_bit")
    assert too_many.value.code == "too_many_frames"
    # Exactly MAX_AUDIT_FRAMES selections are accepted.
    batch = audit_frames([selection] * MAX_AUDIT_FRAMES, mandated_contract,
                         model="single_bit")
    assert len(batch.responses) == MAX_AUDIT_FRAMES


def test_audit_output_does_not_disclose_full_frames(mandated_contract):
    batch = audit_frames(
        [(MANDATED_REQUEST, ORIGINAL_RESPONSE)], mandated_contract,
        model="multi_bit_4",
    )
    text = json.dumps(batch.as_dict(), ensure_ascii=False)
    assert ORIGINAL_RESPONSE.hex() not in text
    assert MUTATED_RESPONSE.hex() not in text
    minimal = batch.responses[0].minimal_silent
    assert "...(18 hex chars)" in minimal.original_summary
    assert "...(18 hex chars)" in minimal.mutated_summary


def test_store_selection_and_report_integration(demo_run):
    store = Store(demo_run.paths.db, read_only=True)
    try:
        contract = load_contract(demo_run.paths.contract)
        batch_id = demo_run.result.batch_id
        good_lines = [
            row["line_no"]
            for row in store.conn.execute(
                "SELECT line_no FROM frames WHERE batch_id = ? "
                "AND quality_code = 'good' ORDER BY line_no",
                (batch_id,),
            )
        ]
        assert 1 <= len(good_lines) <= MAX_AUDIT_FRAMES
        audited = audit_store_frames(
            store, batch_id, contract, good_lines, model="single_bit"
        )
        report = audited.as_dict()
        assert report["frame_count"] == len(good_lines)
        assert report["risk"]["silent_possible"] is False
        # Selection order does not change the outcome.
        shuffled = list(reversed(good_lines))
        assert audit_store_frames(
            store, batch_id, contract, shuffled, model="single_bit"
        ).as_dict() == report

        # Non-good frames and oversize selections are rejected explicitly.
        bad_line = store.conn.execute(
            "SELECT line_no FROM frames WHERE batch_id = ? "
            "AND quality_code != 'good' ORDER BY line_no LIMIT 1",
            (batch_id,),
        ).fetchone()
        with pytest.raises(AuditError) as not_auditable:
            audit_store_frames(
                store, batch_id, contract, [bad_line["line_no"]], model="single_bit"
            )
        assert not_auditable.value.code == "frame_not_auditable"
        with pytest.raises(AuditError) as too_many:
            audit_store_frames(
                store, batch_id, contract,
                list(range(1, MAX_AUDIT_FRAMES + 2)),
                model="single_bit",
            )
        assert too_many.value.code == "too_many_frames"

        # The hypothetical audit rides along in the summary without changing
        # any batch fact, quality distribution or pending-recollect list.
        baseline_summary = collect_summary(store, batch_id, contract)
        assert "crc_audit" not in baseline_summary
        enriched = collect_summary(store, batch_id, contract, crc_audit=report)
        assert enriched["crc_audit"] == report
        for key in (
            "batch",
            "totals",
            "quality_code_distribution",
            "issue_distribution",
            "devices",
            "points",
            "pending_recollect",
        ):
            assert enriched[key] == baseline_summary[key]
    finally:
        store.close()
