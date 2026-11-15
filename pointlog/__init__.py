"""Repository-local package wrapper for the src-layout pointlog package."""

from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REAL_PACKAGE = _HERE.parent / "src" / "pointlog"
__path__ = [str(_HERE), str(_REAL_PACKAGE)]
__version__ = "0.1.0"
