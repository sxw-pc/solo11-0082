"""Thin repository entry point for deterministic fixture generation."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pointlog.fixtures_gen import main

if __name__ == "__main__":
    raise SystemExit(main())
