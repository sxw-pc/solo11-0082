import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) in sys.path:
    sys.path.remove(str(SRC))
sys.path.insert(0, str(SRC))

from pointlog.fixtures_gen import generate_demo_replay
from pointlog.pipeline import run_capture


@pytest.fixture
def demo_paths(tmp_path):
    fixture = tmp_path / "replay_demo.jsonl"
    db = tmp_path / "pointlog.db"
    reports = tmp_path / "reports"
    generate_demo_replay(ROOT / "config" / "points.yaml", fixture)
    return SimpleNamespace(
        fixture=fixture,
        db=db,
        reports=reports,
        contract=ROOT / "config" / "points.yaml",
    )


@pytest.fixture
def demo_run(demo_paths):
    result = run_capture(
        replay_path=demo_paths.fixture,
        contract_path=demo_paths.contract,
        db_path=demo_paths.db,
        reports_dir=demo_paths.reports,
    )
    return SimpleNamespace(paths=demo_paths, result=result)
