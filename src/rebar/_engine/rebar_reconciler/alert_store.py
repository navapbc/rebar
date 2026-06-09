from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# fcntl is POSIX-only; CI runs on Linux/macOS so we guard the import. On
# Windows (unsupported by the bridge CI) we fall back to a no-op lock — the
# caller still gets line-buffered append() semantics, just without the
# cross-process serialization guarantee.
if sys.platform != "win32":
    import fcntl
else:  # pragma: no cover - Windows fallback, not exercised by CI
    fcntl = None  # type: ignore[assignment]

_24H_NS = 24 * 3600 * 1_000_000_000

# Canonical state-directory layout for the dso reconciler. The two-level
# bridge_state/<feature> structure is part of the documented bridge contract
# (see the bridge README). Consuming projects override the *location* by
# passing a different repo_root; the layout itself is fixed.
_STATE_SUBDIR = "bridge_state"
_ALERTS_SUBDIR = "bridge_alerts"


def _store_dir(repo_root: Path) -> Path:
    return repo_root / _STATE_SUBDIR / _ALERTS_SUBDIR


def _today_file(repo_root: Path) -> Path:
    from datetime import datetime, timezone

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _store_dir(repo_root) / f"{date_str}.jsonl"


def append(record: dict, repo_root: Path) -> None:
    """Append a record to today's JSONL alert log.

    Concurrent writers from multiple reconciler instances serialize on an
    advisory ``fcntl.LOCK_EX`` flock so line boundaries cannot interleave.
    The lock is released automatically when the file descriptor closes at
    the end of the ``with`` block (POSIX semantics).
    """
    store_dir = _store_dir(repo_root)
    store_dir.mkdir(parents=True, exist_ok=True)
    today = _today_file(repo_root)
    with today.open("a", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def is_deduped(key: str, repo_root: Path, window_ns: int = _24H_NS) -> bool:
    """Return True if an unresolved alert with this key was written within the window."""
    store_dir = _store_dir(repo_root)
    if not store_dir.is_dir():
        return False
    now = time.time_ns()
    for jf in sorted(store_dir.glob("*.jsonl")):
        try:
            for line in jf.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("key") == key and not rec.get("resolved"):
                    ts = rec.get("timestamp_ns", 0)
                    if now - ts <= window_ns:
                        return True
        except Exception:
            continue
    return False


def _atomic_write(path: Path, content: str) -> None:
    """Replace `path` atomically with `content`.

    Writes via tempfile + fsync + os.replace so a crash mid-write cannot leave
    the destination truncated or partially written — the prior `write_text`
    approach truncated first and could lose the whole file on SIGKILL/OOM.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=str(parent), prefix=f".{path.name}.tmp.")
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None  # type: ignore[assignment]
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def patch_bug_filed(key: str, bug_ticket_id: str, repo_root: Path) -> None:
    """Patch the latest unresolved record for key with bug_ticket_id.

    The rewrite is atomic (tempfile + os.replace + fsync) so a crash mid-write
    cannot lose the day's alert history. Non-dict JSONL payloads (e.g. a bare
    number from a corrupt writer) are preserved verbatim rather than crashing
    the patch attempt — the inner guard checks isinstance(rec, dict) before
    accessing rec.get().

    Lines that are NOT the patch target are preserved BYTE-IDENTICAL to their
    original text — we never re-serialize via json.dumps for non-target lines.
    This guarantees that key order, whitespace, ensure_ascii encoding, and any
    other writer-specific formatting stay stable across the patch operation,
    so a crash mid-write cannot leave the file in a "some lines re-serialized,
    some original" mixed-formatting state.
    """
    store_dir = _store_dir(repo_root)
    if not store_dir.is_dir():
        return
    for jf in sorted(store_dir.glob("*.jsonl"), reverse=True):
        out_lines: list[str] = []
        patched = False
        try:
            for line in jf.read_text(encoding="utf-8").splitlines():
                rec = None
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    pass  # malformed JSONL line — skip and preserve original text
                if (
                    not patched
                    and isinstance(rec, dict)
                    and rec.get("key") == key
                    and not rec.get("resolved")
                ):
                    rec["bug_ticket_id"] = bug_ticket_id
                    rec["op"] = "bug_filed"
                    patched = True
                    # Only the patched line is re-serialized; all other lines
                    # are written back byte-identical to their original input.
                    out_lines.append(json.dumps(rec))
                else:
                    out_lines.append(line)
            if patched:
                _atomic_write(jf, "\n".join(out_lines) + "\n")
                return
        except Exception:
            continue
