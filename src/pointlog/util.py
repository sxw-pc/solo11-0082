"""Small deterministic helpers shared by the pointlog modules."""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "points.yaml"
DEFAULT_FIXTURE_PATH = PROJECT_ROOT / "fixtures" / "replay_demo.jsonl"
DEFAULT_DB_PATH = PROJECT_ROOT / "var" / "pointlog.db"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"


def utc_now_iso() -> str:
    """Return a second-resolution UTC timestamp with a trailing Z."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_parent(path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def atomic_write_text(path: str | Path, text: str) -> Path:
    """Write text atomically in the target directory (works on Windows)."""
    target = ensure_parent(path)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return target


def frame_summary(value: str | bytes, limit: int = 48) -> str:
    """Build a short, deterministic frame excerpt for issue rows."""
    if isinstance(value, bytes):
        text = value.hex()
    else:
        text = "".join(str(value).split())
    if not text:
        return "<empty>"
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...({len(text)} hex chars)"


def normalize_number(value: float | int) -> float | int:
    rounded = round(float(value), 6)
    if rounded == int(rounded) and abs(rounded) < 1e15:
        return int(rounded)
    return rounded
