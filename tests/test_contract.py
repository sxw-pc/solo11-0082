import copy
from pathlib import Path

import pytest
import yaml

from pointlog.contract import ContractError, load_contract, parse_contract

CONFIG = Path(__file__).resolve().parents[1] / "config" / "points.yaml"
BASE = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def contract_text(mutator):
    raw = copy.deepcopy(BASE)
    mutator(raw)
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)


def point(raw, key):
    for device in raw["devices"]:
        for item in device["points"]:
            if item["key"] == key:
                return item
    raise AssertionError(key)


def test_loads_demo_contract():
    contract = load_contract(CONFIG)
    assert contract.version == 1
    assert len(contract.devices) == 2
    assert len(contract.points) == 9
    speed = contract.get("line_a_speed")
    assert speed.device_id == "demo-gateway"
    assert speed.slave_id == 1
    assert speed.address_end == 1
    assert speed.enum == {}
    assert contract.get("line_a_mode").enum[1] == "run"
    assert contract.find(1, 3, 0, 2) is speed
    assert contract.find(1, 3, 0, 1) is None
    assert contract.device_label("line-a") == "Line A synthetic remote I/O"
    assert contract.device_label("missing") == "missing"


def test_demo_contract_recollect_fields():
    contract = load_contract(CONFIG)
    recollect = contract.recollect
    assert recollect is not None
    assert recollect.objective == "coverage"
    assert recollect.max_requests == 4
    assert recollect.device_cooldown_s == 0.25
    assert recollect.request_cost_s == 0.05
    assert recollect.window_seconds == 30
    speed = contract.get("line_a_speed")
    assert speed.recollect_priority == 5
    assert speed.recollect_ttl_seconds == 3600
    mode = contract.get("line_a_mode")
    assert mode.recollect_priority is None
    assert mode.recollect_ttl_seconds is None


def test_contract_without_recollect_section_parses_as_none():
    def mutate(raw):
        raw.pop("recollect")
        for device in raw["devices"]:
            for item in device["points"]:
                item.pop("recollect_priority", None)
                item.pop("recollect_ttl_seconds", None)

    contract = parse_contract(contract_text(mutate))
    assert contract.recollect is None
    assert all(point.recollect_priority is None for point in contract.points)


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda raw: raw.update(version=2), "contract_version"),
        (lambda raw: raw.pop("devices"), "contract_devices"),
        (lambda raw: raw["devices"].__setitem__(1, copy.deepcopy(raw["devices"][0])),
         "contract_device"),
        (lambda raw: point(raw, "line_a_speed").update(slave_id=0), "contract_slave"),
        (lambda raw: point(raw, "line_a_speed").update(function=6), "contract_function"),
        (lambda raw: point(raw, "line_a_speed").update(start=70000), "contract_address"),
        (lambda raw: point(raw, "line_a_speed").update(start=65535), "contract_address"),
        (lambda raw: point(raw, "line_a_speed").update(data_type="float64"),
         "contract_data_type"),
        (lambda raw: point(raw, "line_a_speed").update(count=1), "contract_register_count"),
        (lambda raw: point(raw, "line_a_speed").update(byte_order="BA"),
         "contract_byte_order"),
        (lambda raw: point(raw, "line_a_speed").update(byte_order="NOPE"),
         "contract_byte_order"),
        (lambda raw: point(raw, "line_a_speed").update(scale=0), "contract_scale"),
        (lambda raw: point(raw, "line_a_speed").update(offset="zero"), "contract_offset"),
        (lambda raw: point(raw, "line_a_speed").update(range=[10, 1]), "contract_range"),
        (lambda raw: point(raw, "line_a_speed").update(range=[0]), "contract_range"),
        (lambda raw: point(raw, "line_a_speed").update(key="bad key"), "contract_point"),
        (lambda raw: point(raw, "line_a_mode").update(enum={"x": "bad"}),
         "contract_enum"),
        (lambda raw: point(raw, "line_a_mode").update(enum={0: ""}), "contract_enum"),
        (lambda raw: point(raw, "line_a_flow").update(enum={0: "zero"}), "contract_enum"),
        (lambda raw: point(raw, "line_a_speed").update(recollect_priority=10),
         "contract_recollect"),
        (lambda raw: point(raw, "line_a_speed").update(recollect_priority="high"),
         "contract_recollect"),
        (lambda raw: point(raw, "line_a_speed").update(recollect_ttl_seconds=0),
         "contract_recollect"),
        (lambda raw: point(raw, "line_a_speed").update(recollect_ttl_seconds="1h"),
         "contract_recollect"),
        (lambda raw: raw["recollect"].update(objective="balanced"),
         "contract_recollect"),
        (lambda raw: raw["recollect"].update(max_requests=-1),
         "contract_recollect"),
        (lambda raw: raw["recollect"].update(max_requests=1.5),
         "contract_recollect"),
        (lambda raw: raw["recollect"].pop("max_requests"),
         "contract_recollect"),
        (lambda raw: raw["recollect"].update(device_cooldown_s=-0.5),
         "contract_recollect"),
        (lambda raw: raw["recollect"].update(request_cost_s=0),
         "contract_recollect"),
        (lambda raw: raw["recollect"].update(window_seconds="30"),
         "contract_recollect"),
        (lambda raw: raw.update(recollect=["coverage"]),
         "contract_recollect"),
    ],
)
def test_static_validation_rejects_bad_contracts(mutator, code):
    with pytest.raises(ContractError) as excinfo:
        parse_contract(contract_text(mutator), source="synthetic.yaml")
    assert excinfo.value.code == code


def test_duplicate_point_key_is_rejected():
    def mutate(raw):
        flow = point(raw, "line_a_flow")
        flow["key"] = "line_a_speed"
        flow["start"] = 80

    with pytest.raises(ContractError) as excinfo:
        parse_contract(contract_text(mutate))
    assert excinfo.value.code == "contract_duplicate_key"


def test_duplicate_match_key_is_rejected():
    def mutate(raw):
        point(raw, "line_a_flow")["slave_id"] = 1
        point(raw, "line_a_flow")["function"] = 3
        point(raw, "line_a_flow")["start"] = 0
        point(raw, "line_a_flow")["count"] = 2
        point(raw, "line_a_flow")["data_type"] = "uint32"
        point(raw, "line_a_flow")["byte_order"] = "ABCD"

    with pytest.raises(ContractError) as excinfo:
        parse_contract(contract_text(mutate))
    assert excinfo.value.code == "contract_duplicate_match"


def test_invalid_yaml_and_root_are_rejected():
    with pytest.raises(ContractError) as excinfo:
        parse_contract("version: [")
    assert excinfo.value.code == "contract_yaml"
    with pytest.raises(ContractError) as excinfo:
        parse_contract("- not-a-map")
    assert excinfo.value.code == "contract_root"


def test_load_missing_file_is_contract_error(tmp_path):
    with pytest.raises(ContractError) as excinfo:
        load_contract(tmp_path / "missing.yaml")
    assert excinfo.value.code == "contract_read"
