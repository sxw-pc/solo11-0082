"""Static ``mode_state`` contract validation."""

from __future__ import annotations

import copy

import pytest
import yaml

from pointlog.contract import ContractError, parse_contract

BASE = """
version: 1
devices:
  - id: rig
    points:
      - key: mode
        slave_id: 7
        function: 3
        start: 10
        count: 1
        data_type: uint16
        byte_order: AB
        scale: 1
        offset: 0
        range: [0, 5]
        enum:
          0: idle
          1: warmup
          2: run
          3: fault
        mode_state:
{block}
"""

VALID = """          verification: hop
          max_gap_seconds: 0
          min_dwell_seconds:
            1: 120
          reset_state: 3
          transitions:
            0: [1, 3]
            1: [2, 3]
            2: [0, 3]
            3: [0]
"""


def contract_text(block: str) -> str:
    return BASE.format(block=block)


def _expect(block: str, code: str):
    with pytest.raises(ContractError) as excinfo:
        parse_contract(contract_text(block), source="mode.yaml")
    assert excinfo.value.code == code
    return excinfo.value


def test_valid_mode_state_is_parsed():
    contract = parse_contract(contract_text(VALID), source="mode.yaml")
    point = contract.get("mode")
    mode = point.mode_state
    assert mode is not None
    assert mode.point_key == "mode"
    assert mode.verification == "hop"
    assert mode.max_gap_seconds == 0
    assert mode.min_dwell_seconds == {1: 120}
    assert mode.reset_state == 3
    assert mode.transitions[0] == (1, 3)
    assert mode.states == (0, 1, 2, 3)


def test_bounded_mode_state_is_parsed():
    block = VALID.replace("verification: hop", "verification: bounded").replace(
        "max_gap_seconds: 0", "max_gap_seconds: 900"
    )
    mode = parse_contract(contract_text(block)).get("mode").mode_state
    assert mode.verification == "bounded"
    assert mode.max_gap_seconds == 900


def test_mode_state_absent_is_none():
    text = """
version: 1
devices:
  - id: rig
    points:
      - key: mode
        slave_id: 7
        function: 3
        start: 10
        count: 1
        data_type: uint16
        byte_order: AB
        scale: 1
        offset: 0
        range: [0, 5]
        enum: {0: idle}
"""
    assert parse_contract(text).get("mode").mode_state is None


def test_transition_referencing_unknown_enum_value_is_rejected():
    block = VALID.replace("1: [2, 3]", "1: [2, 9]")
    error = _expect(block, "contract_mode_enum")
    assert "9" in error.detail


def test_enum_state_without_exit_is_rejected():
    block = VALID.replace("2: [0, 3]", "2: []")
    _expect(block, "contract_mode")


def test_reachable_dead_end_is_rejected():
    block = VALID.replace("2: [0, 3]", "").replace("1: [2, 3]", "1: [2]")
    _expect(block, "contract_mode_dead_end")


def test_terminal_state_can_have_no_exit():
    block = VALID.replace("2: [0, 3]", "").replace(
        "transitions:", "terminal_states: [2]\n          transitions:"
    )
    mode = parse_contract(contract_text(block)).get("mode").mode_state
    assert mode is not None
    assert 2 in mode.terminal_states


def test_terminal_state_may_not_also_transition():
    block = VALID.replace(
        "transitions:", "terminal_states: [2]\n          transitions:"
    )
    _expect(block, "contract_mode")


def test_dwell_for_unreachable_state_is_rejected():
    block = VALID.replace(
        "min_dwell_seconds:\n            1: 120",
        "min_dwell_seconds:\n            1: 120\n            4: 60",
    )
    # 4 is not in the enum, so this is caught before dwell reachability.
    _expect(block, "contract_mode_enum")


def test_negative_dwell_is_rejected():
    block = VALID.replace("1: 120", "1: -5")
    _expect(block, "contract_mode_dwell")


def test_dwell_on_state_with_no_entry_is_rejected():
    # Remove every edge into 0 while keeping it as a source.
    block = (VALID.replace("2: [0, 3]", "2: [3]")
             .replace("3: [0]", "3: [1]")
             .replace("min_dwell_seconds:\n            1: 120",
                      "min_dwell_seconds:\n            0: 120"))
    _expect(block, "contract_mode_dwell")


def test_dwell_off_any_cycle_is_rejected():
    block = """          verification: hop
          transitions:
            0: [1]
            1: [2]
            2: [3]
          min_dwell_seconds:
            1: 120
          terminal_states: [3]
"""
    _expect(block, "contract_mode_dwell")


def test_reset_must_have_declared_outlet_and_inlet():
    # 3 keeps an outgoing edge but nothing enters it, so it cannot be reached
    # through any declared edge and may not be called a reset state.
    block = (VALID.replace("0: [1, 3]", "0: [1]")
             .replace("1: [2, 3]", "1: [2]")
             .replace("2: [0, 3]", "2: [0]"))
    _expect(block, "contract_mode_reset")


def test_reset_on_isolated_state_is_rejected():
    block = VALID.replace("reset_state: 3", "reset_state: 0")
    # 0 is a legal source and target, so reset itself is fine here.
    mode = parse_contract(contract_text(block)).get("mode").mode_state
    assert mode.reset_state == 0


def test_max_gap_must_be_non_negative_integer():
    block = VALID.replace("max_gap_seconds: 0", "max_gap_seconds: -1")
    _expect(block, "contract_mode_gap")
    block2 = VALID.replace("max_gap_seconds: 0", "max_gap_seconds: 1.5")
    _expect(block2, "contract_mode_gap")


def test_verification_must_be_known():
    block = VALID.replace("verification: hop", "verification: strict")
    _expect(block, "contract_mode")


def test_transitions_must_be_a_mapping():
    block = VALID.replace(
        "transitions:\n            0: [1, 3]\n            1: [2, 3]\n"
        "            2: [0, 3]\n            3: [0]",
        "transitions: [0, 1]",
    )
    _expect(block, "contract_mode")


def test_mode_state_requires_enum():
    text = """
version: 1
devices:
  - id: rig
    points:
      - key: mode
        slave_id: 7
        function: 3
        start: 10
        count: 1
        data_type: uint16
        byte_order: AB
        scale: 1
        offset: 0
        range: [0, 5]
        mode_state:
          verification: hop
          transitions:
            0: [1]
            1: [0]
"""
    with pytest.raises(ContractError) as excinfo:
        parse_contract(text)
    assert excinfo.value.code == "contract_mode"


def test_rejected_before_any_batch_is_written(tmp_path):
    from pointlog.pipeline import run_capture

    contract = tmp_path / "bad.yaml"
    contract.write_text(contract_text(VALID.replace("1: [2, 3]", "1: [2, 9]")),
                        encoding="utf-8")
    replay = tmp_path / "r.jsonl"
    replay.write_text("", encoding="utf-8")
    database = tmp_path / "never.db"
    with pytest.raises(ContractError):
        run_capture(replay_path=replay, contract_path=contract, db_path=database,
                    reports_dir=None)
    assert not database.exists()
