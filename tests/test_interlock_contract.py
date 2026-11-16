"""Contract-level validation of the two-point interlock rule schema."""

import copy
import re

import pytest
import yaml

from pointlog.contract import (
    ContractError,
    DIRECTION_ABOVE,
    DIRECTION_BELOW,
    SAMPLING_BOUNDED_HOLD,
    SAMPLING_STRICT_SIMULTANEOUS,
    parse_contract,
)

CONFIG = __import__("pathlib").Path(__file__).resolve().parents[1] / "config" / "points.yaml"
BASE = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _contract(mutator=None):
    raw = copy.deepcopy(BASE)
    if mutator:
        mutator(raw)
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)


def _device(raw, device_id):
    return next(item for item in raw["devices"] if item["id"] == device_id)


def _rule(raw):
    return _device(raw, "line-a")["interlocks"][0]


def test_demo_contract_declares_one_interlock():
    contract = parse_contract(_contract())
    assert len(contract.interlocks) == 1
    rule = contract.interlocks[0]
    assert rule.rule_id == "line_a_pressure_level"
    assert rule.version == 1
    assert rule.device_id == "line-a"
    assert rule.sampling == SAMPLING_BOUNDED_HOLD
    assert rule.min_duration_minutes == 2
    assert rule.max_stale_minutes == 5
    assert rule.point_keys == ("line_a_pressure", "line_a_level")
    assert rule.points[0].threshold == 150
    assert rule.points[0].direction == DIRECTION_ABOVE
    assert rule.points[0].is_triggered(151)
    assert not rule.points[0].is_triggered(150)
    assert contract.interlocks_for("line-a") == [rule]
    assert contract.interlocks_for("demo-gateway") == []
    assert contract.interlock("line_a_pressure_level") is rule
    assert contract.interlock("missing") is None


def test_strict_simultaneous_and_below_direction_are_accepted():
    def mutate(raw):
        rule = _rule(raw)
        rule["sampling"] = "strict_simultaneous"
        rule["points"][0]["direction"] = "below"
        rule["points"][0]["threshold"] = 5
        rule["points"][0]["inclusive"] = True

    contract = parse_contract(_contract(mutate))
    rule = contract.interlocks[0]
    assert rule.sampling == SAMPLING_STRICT_SIMULTANEOUS
    assert rule.points[0].direction == DIRECTION_BELOW
    assert rule.points[0].inclusive
    assert rule.points[0].is_triggered(5)
    assert rule.points[0].is_triggered(4.9)
    assert not rule.points[0].is_triggered(5.1)


def test_sampling_aliases_normalise():
    for alias, expected in [
        ("bounded", SAMPLING_BOUNDED_HOLD),
        ("hold", SAMPLING_BOUNDED_HOLD),
        ("strict", SAMPLING_STRICT_SIMULTANEOUS),
        ("same_timestamp", SAMPLING_STRICT_SIMULTANEOUS),
    ]:
        def mutate(raw, alias=alias):
            _rule(raw)["sampling"] = alias

        assert parse_contract(_contract(mutate)).interlocks[0].sampling == expected


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda raw: _rule(raw).update(points=[_rule(raw)["points"][0]]),
         "contract_interlock"),
        (lambda raw: _rule(raw).update(points=[
            _rule(raw)["points"][0], _rule(raw)["points"][0]]),
         "contract_interlock_point"),
        (lambda raw: _rule(raw)["points"][1].update(key="line_a_temperature"),
         "contract_interlock_point"),
        (lambda raw: _rule(raw)["points"][1].update(key="no_such_point"),
         "contract_interlock_point"),
        (lambda raw: _rule(raw).update(min_duration_minutes=0),
         "contract_interlock_duration"),
        (lambda raw: _rule(raw).update(min_duration_minutes=-1),
         "contract_interlock_duration"),
        (lambda raw: _rule(raw).update(min_duration_minutes="two"),
         "contract_interlock_duration"),
        (lambda raw: _rule(raw).update(max_stale_minutes=0),
         "contract_interlock_stale"),
        (lambda raw: _rule(raw).update(sampling="whenever"),
         "contract_interlock_sampling"),
        (lambda raw: _rule(raw).update(version=0), "contract_interlock"),
        (lambda raw: _rule(raw).update(version="one"), "contract_interlock"),
        (lambda raw: _rule(raw).update(id="bad id"), "contract_interlock"),
        (lambda raw: _rule(raw)["points"][0].update(threshold="hot"),
         "contract_interlock_threshold"),
        (lambda raw: _rule(raw)["points"][0].update(direction="sideways"),
         "contract_interlock_direction"),
        (lambda raw: _rule(raw)["points"][0].update(inclusive="yes"),
         "contract_interlock_direction"),
        (lambda raw: _rule(raw).update(description=5), "contract_interlock"),
    ],
)
def test_invalid_interlock_rules_are_rejected(mutator, code):
    with pytest.raises(ContractError) as excinfo:
        parse_contract(_contract(mutator), source="interlock.yaml")
    assert excinfo.value.code == code


def test_duplicate_rule_id_is_rejected():
    def mutate(raw):
        device = _device(raw, "line-a")
        duplicate = copy.deepcopy(device["interlocks"][0])
        duplicate["points"] = [
            {"key": "line_a_voltage", "threshold": 1, "direction": "above"},
            {"key": "line_a_energy", "threshold": 1, "direction": "above"},
        ]
        device["interlocks"].append(duplicate)

    with pytest.raises(ContractError) as excinfo:
        parse_contract(_contract(mutate))
    assert excinfo.value.code == "contract_duplicate_interlock"


def test_interlock_must_belong_to_the_declaring_device():
    def mutate(raw):
        # Move the demo rule to the other device but keep foreign point keys.
        rule = _device(raw, "line-a").pop("interlocks")
        _device(raw, "demo-gateway")["interlocks"] = rule

    with pytest.raises(ContractError) as excinfo:
        parse_contract(_contract(mutate))
    assert excinfo.value.code == "contract_interlock_point"


def test_contract_without_interlocks_stays_valid():
    def mutate(raw):
        for device in raw["devices"]:
            device.pop("interlocks", None)

    contract = parse_contract(_contract(mutate))
    assert contract.interlocks == ()
    assert contract.interlocks_for("line-a") == []
