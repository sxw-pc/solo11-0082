"""JSONL replay-file fingerprinting, version fencing and line iteration."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .util import sha256_file


class ReplayError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


# Version-fence route implemented by this module: the source file is copied
# into a private, content-addressed, read-only snapshot ("sealed copy") and
# every downstream read (hash, line count, per-line content) is served from
# that snapshot instead of the mutable source path.
SEAL_ROUTE = "sealed_copy"

_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ReplayLine:
    line_no: int
    raw_text: str
    ts: str
    request_hex: str
    response_hex: str
    source: str
    error_code: str | None = None
    error_detail: str = ""

    @property
    def valid(self) -> bool:
        return self.error_code is None


@dataclass(frozen=True)
class ReplaySource:
    """A replay input pinned to one immutable byte version.

    ``path`` is the original source path (batch identity, resume-state key and
    report label).  ``read_path`` is the private sealed copy that actually
    backs :meth:`iter_lines`; when it is ``None`` the source path itself is
    read (legacy callers).  ``sha256``/``total_lines``/``byte_count`` always
    describe the same bytes that ``read_path`` serves.
    """

    path: Path
    sha256: str
    total_lines: int
    byte_count: int = 0
    read_path: Path | None = None

    def iter_lines(self, skip_lines: int = 0) -> Iterator[ReplayLine]:
        if skip_lines < 0:
            raise ReplayError("invalid_offset", "skip_lines must not be negative")
        target = self.read_path if self.read_path is not None else self.path
        try:
            with target.open("r", encoding="utf-8", newline=None) as handle:
                for line_no, raw in enumerate(handle, start=1):
                    if line_no <= skip_lines:
                        continue
                    yield parse_line(line_no, raw)
        except OSError as exc:
            raise ReplayError("replay_read", f"cannot read replay file: {exc}") from exc


def parse_line(line_no: int, raw: str) -> ReplayLine:
    text = raw.rstrip("\r\n")
    if not text.strip():
        return ReplayLine(
            line_no, text, "", "", "", "", "invalid_line", "line is blank"
        )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return ReplayLine(
            line_no, text, "", "", "", "", "invalid_json", f"invalid JSON: {exc.msg}"
        )
    if not isinstance(value, dict):
        return ReplayLine(
            line_no, text, "", "", "", "", "invalid_line", "line must be a JSON object"
        )

    ts = value.get("ts")
    request_hex = value.get("request_hex")
    response_hex = value.get("response_hex")
    source = value.get("source")
    errors: list[str] = []
    if not isinstance(ts, str) or not ts.strip():
        errors.append("ts must be a non-empty string")
    if not isinstance(request_hex, str) or not request_hex.strip():
        errors.append("request_hex must be a non-empty string")
    if not isinstance(response_hex, str):
        errors.append("response_hex must be a string (empty is allowed)")
    if not isinstance(source, str) or not source.strip():
        errors.append("source must be a non-empty string")
    if errors:
        return ReplayLine(
            line_no,
            text,
            ts if isinstance(ts, str) else "",
            request_hex if isinstance(request_hex, str) else "",
            response_hex if isinstance(response_hex, str) else "",
            source if isinstance(source, str) else "",
            "invalid_line",
            "; ".join(errors),
        )
    return ReplayLine(
        line_no=line_no,
        raw_text=text,
        ts=ts.strip(),
        request_hex=request_hex,
        response_hex=response_hex,
        source=source.strip(),
    )


def count_lines(path: str | Path) -> int:
    try:
        with Path(path).open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError as exc:
        raise ReplayError("replay_read", f"cannot read replay file: {exc}") from exc


def sealed_copy_path(seal_dir: str | Path, sha256: str) -> Path:
    """Location of the private immutable copy for one content digest."""
    return Path(seal_dir) / f"{sha256}.jsonl"


def _hash_count_bytes(path: str | Path) -> tuple[str, int, int]:
    """Stream one file and return (sha256, line count, byte count).

    The line count matches :func:`count_lines` semantics (a final line without
    a trailing newline still counts) while the file is read exactly once in
    bounded chunks.
    """
    digest = hashlib.sha256()
    lines = 0
    nbytes = 0
    last_byte = b""
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
            lines += chunk.count(b"\n")
            nbytes += len(chunk)
            last_byte = chunk[-1:]
    if nbytes and last_byte != b"\n":
        lines += 1
    return digest.hexdigest(), lines, nbytes


def _seal_source(source: Path, seal_dir: Path) -> tuple[Path, str, int, int]:
    """Copy ``source`` into ``seal_dir`` in one streaming pass.

    The digest and line count are computed from the same bytes as they are
    written, the copy is atomically renamed to its content-addressed final
    name and marked read-only (best effort).  Returns
    ``(sealed_path, sha256, total_lines, byte_count)``.
    """
    seal_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    lines = 0
    nbytes = 0
    last_byte = b""
    fd, temp_name = tempfile.mkstemp(prefix=".seal-", dir=str(seal_dir))
    try:
        with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
            for chunk in iter(lambda: src.read(_CHUNK_SIZE), b""):
                dst.write(chunk)
                digest.update(chunk)
                lines += chunk.count(b"\n")
                nbytes += len(chunk)
                last_byte = chunk[-1:]
        if nbytes and last_byte != b"\n":
            lines += 1
        sha256 = digest.hexdigest()
        final = sealed_copy_path(seal_dir, sha256)
        os.replace(temp_name, final)
        try:
            os.chmod(final, 0o444)
        except OSError:
            pass  # read-only marking is best effort, not a security boundary
        return final, sha256, lines, nbytes
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def load_replay(path: str | Path, seal_dir: str | Path) -> ReplaySource:
    """Fingerprint ``path`` and pin it behind the version fence.

    The source is hashed and counted, then sealed into a private copy in a
    single streaming pass.  If the bytes observed by these reads disagree, the
    file changed under us and no trustworthy version exists: a
    :class:`ReplayError` with code ``replay_unstable`` is raised and no batch
    identity is derived.  On success the returned source serves every further
    read from the sealed copy, so the digest, line count and per-line content
    are guaranteed to describe one immutable byte version.
    """
    target = Path(path)
    if not target.is_file():
        raise ReplayError("replay_missing", f"replay file does not exist: {target}")
    try:
        digest = sha256_file(target)
    except OSError as exc:
        raise ReplayError("replay_read", f"cannot hash replay file: {exc}") from exc
    total_lines = count_lines(target)
    try:
        sealed_path, sealed_sha, sealed_lines, nbytes = _seal_source(
            target, Path(seal_dir)
        )
    except OSError as exc:
        raise ReplayError("replay_read", f"cannot seal replay file: {exc}") from exc
    if sealed_sha != digest or sealed_lines != total_lines:
        raise ReplayError(
            "replay_unstable",
            "replay file changed while it was being fingerprinted "
            f"(sha256 {digest[:12]}… → {sealed_sha[:12]}…, "
            f"lines {total_lines} → {sealed_lines}); refusing to derive a "
            "batch identity from a moving source",
        )
    return ReplaySource(
        path=target,
        sha256=sealed_sha,
        total_lines=sealed_lines,
        byte_count=nbytes,
        read_path=sealed_path,
    )


def verify_copy(
    path: str | Path, *, sha256: str, total_lines: int
) -> tuple[bool, str, int]:
    """Re-check a sealed copy against its recorded identity.

    Streams the whole file (bounded memory) and compares the full-content
    digest and line count — never the name, length, mtime or a prefix.
    Returns ``(ok, detail, byte_count)``.
    """
    try:
        actual_sha, actual_lines, nbytes = _hash_count_bytes(path)
    except OSError as exc:
        return False, f"cannot read sealed copy: {exc}", 0
    if actual_sha != sha256:
        return (
            False,
            f"sealed copy digest mismatch ({actual_sha[:12]}… != {sha256[:12]}…)",
            nbytes,
        )
    if actual_lines != total_lines:
        return (
            False,
            f"sealed copy line count mismatch ({actual_lines} != {total_lines})",
            nbytes,
        )
    return True, "sealed copy verified", nbytes


def source_drift_status(path: str | Path, sha256: str) -> str:
    """Compare the current source bytes with the processed version digest.

    Returns ``match``/``changed``/``missing``/``unreadable``.  The comparison
    is a full-content hash, never a name/length/mtime/prefix shortcut.
    """
    target = Path(path)
    if not target.is_file():
        return "missing"
    try:
        current = sha256_file(target)
    except OSError:
        return "unreadable"
    return "match" if current == sha256 else "changed"
